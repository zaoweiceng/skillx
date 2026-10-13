"""Execute candidate graphs against explicit examples before activation."""

import asyncio
from collections import defaultdict
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from app.application.capabilities import resolve_package_path
from app.application.compiler import WorkflowCompiler
from app.application.expressions import resolve_mapping
from app.infrastructure.database import CapabilityVersion
from app.runtime.execution_contracts import CancellationToken, RunContext
from app.runtime.executors import ExecutorRegistry
from app.runtime.skillx import package_digest
from app.runtime.skillx_host import execute_in_subprocess
from app.runtime.state_machine import node_readiness


class WorkflowValidationFailure(ValueError):
    def __init__(self, message, observations, counterexample=None):
        super().__init__(message)
        self.observations = observations
        self.counterexample = counterexample


async def validate_candidate(session, definition, examples, evidence, *, generated_ids=frozenset(), usage_sink=None):
    from app.application.learning_evidence import validate_examples
    from app.application.workflow_nesting import expand_workflow

    definition = await expand_workflow(session, definition)
    if any(n["type"] not in {"input", "output", "skillx", "condition", "transform", "llm"}
           for n in definition["spec"]["nodes"]):
        raise ValueError("能力组合包含尚未提供回放证据的节点，保留候选")
    local_versions = set()
    for node in definition["spec"]["nodes"]:
        if node["type"] == "skillx":
            version = await session.get(CapabilityVersion, node["config"]["capabilityVersionId"])
            raw = version.manifest_json["manifest"]["spec"]["interfaces"][node["config"]["interface"]]
            if raw.get("x-executionProfile") == "local-command":
                local_versions.add(version.id)
    validate_examples(examples, evidence, local_command=bool(local_versions))
    compiled = WorkflowCompiler().compile(definition)
    from app.application.runtime_profiles import workflow_profiles
    profiles = await workflow_profiles(session, compiled.definition)
    if any(n.type == "llm" and n.config.get("modelProfile") == "fake" for n in compiled.definition.spec.nodes):
        raise ValueError("工作流提炼验证不能使用 fake 模型")
    nodes = {n.id: n for n in compiled.definition.spec.nodes}
    incoming = defaultdict(list)
    for edge in compiled.definition.spec.edges:
        incoming[edge.target].append(edge)
    from contextlib import ExitStack
    successful_inputs = []
    with ExitStack() as cleanup:
        for example_index, example in enumerate(examples):
            from app.application.schema_defaults import apply_input_defaults
            inputs = apply_input_defaults(example.input, compiled.definition.spec.input_schema)
            workspace = None
            if local_versions:
                from app.application.local_validation import LocalValidation
                workspace = LocalValidation()
                cleanup.callback(workspace.close)
                await workspace.setup(example.setup)
                inputs = await workspace.resolve_inputs(inputs)
            Draft202012Validator(compiled.definition.spec.input_schema).validate(inputs)
            outputs, states, observations = {}, {}, []
            context = RunContext(
                str(uuid4()), inputs, str(uuid4()), CancellationToken(asyncio.Event()),
                runtime_snapshot={"nodeProfiles": profiles}
            )
            for node_id in compiled.topological_order:
                node = nodes[node_id]
                if node.type == "input":
                    outputs[node_id], states[node_id] = inputs, "succeeded"
                    continue
                readiness, _ = node_readiness(
                    incoming[node_id], states, outputs, {n.id: n.on_error for n in nodes.values()}
                )
                if readiness != "ready":
                    states[node_id] = "skipped"
                    continue
                try:
                    node_inputs = resolve_mapping(node.input, inputs, outputs)
                except ValueError as exc:
                    raise WorkflowValidationFailure(f"节点 {node_id} 输入映射失败：{exc}", observations) from exc
                if node.type == "skillx":
                    version = await session.get(CapabilityVersion, node.config["capabilityVersionId"])
                    interface = version.manifest_json["interfaces"][node.config["interface"]]
                    locks = version.manifest_json.get("dependencyLocks", {})
                    pure = interface["kind"] == "direct" and not interface.get("effects") and not locks
                    if version.id in local_versions:
                        from app.domain.workflow import NodeResult
                        package = resolve_package_path(version.package_path)
                        if package_digest(package) != version.digest:
                            raise ValueError("候选 SkillX digest 漂移")
                        try:
                            output = await workspace.execute((package / "execute.py").read_text(), node_inputs, interface["outputSchema"])
                        except (ValueError, ValidationError) as exc:
                            observations.append({"nodeId": node_id, "nodeType": node.type,
                                "capabilityVersionId": version.id, "interface": node.config["interface"],
                                "input": node_inputs, "output": None, "error": str(exc)})
                            raise WorkflowValidationFailure(f"候选节点 {node_id} 验证失败: {exc}", observations) from exc
                        result = NodeResult(status="succeeded", output=output)
                    elif pure:
                        package = resolve_package_path(version.package_path)
                        if package_digest(package) != version.digest:
                            raise ValueError("候选 SkillX digest 漂移")
                        async with asyncio.timeout(30):
                            result = await execute_in_subprocess(
                                package, node.config["interface"], context.node_run_id, node_inputs
                            )
                    elif version.id in generated_ids and interface["kind"] == "direct":
                        import json

                        from app.application.dependency_fixtures import fixture_handler

                        package = resolve_package_path(version.package_path)
                        proof = json.loads((package / "dependency-validation.json").read_text())
                        if proof["locks"] != locks:
                            raise ValueError("生成依赖的验证锁已变化")
                        async with asyncio.timeout(30):
                            result = await execute_in_subprocess(
                                package,
                                node.config["interface"],
                                context.node_run_id,
                                node_inputs,
                                dependency_handler=fixture_handler(proof["fixtures"]),
                            )
                    elif version.id in generated_ids and interface["kind"] in {"llm", "agent"}:
                        import json

                        from app.application.skillx_execution import execute_skillx

                        proof = json.loads(
                            (resolve_package_path(version.package_path) / "validation.json").read_text()
                        )
                        if proof.get("profile") != "model" or proof.get("status") != "passed":
                            raise ValueError("模型候选缺少真实验证记录")
                        result = await execute_skillx(
                            version.id,
                            node.config["interface"],
                            node_inputs,
                            "graph-" + str(uuid4()),
                            proof["runtime"],
                            _candidate_version=version,
                        )
                    else:
                        result = await validate_observed_capability(
                            version, node.config["interface"], node_inputs, evidence, context.node_run_id
                        )
                else:
                    result = await ExecutorRegistry().get(node.type).execute(context, node, node_inputs)
                if usage_sink and node.type == "llm":
                    await usage_sink(context.node_run_id + ":" + node_id, result.metrics)
                observations.append({"nodeId": node_id, "nodeType": node.type,
                    "capabilityVersionId": node.config.get("capabilityVersionId"),
                    "interface": node.config.get("interface"), "input": node_inputs,
                    "output": result.output, "error": str(result.error) if result.error else None})
                if result.status != "succeeded":
                    raise WorkflowValidationFailure(f"候选节点 {node_id} 验证失败: {result.error}", observations)
                outputs[node_id], states[node_id] = result.output, result.status
            output_id = next(n.id for n in nodes.values() if n.type == "output")
            actual = outputs.get(output_id)
            try:
                Draft202012Validator(compiled.definition.spec.output_schema).validate(actual)
                if workspace:
                    await workspace.verify(example, actual, inputs)
                else:
                    from app.application.workflow_assertions import check_expected, verify_pure_output
                    check_expected(example.expected, actual, partial=bool(example.verify.strip()))
                    if example.verify.strip():
                        await verify_pure_output(example, actual, inputs)
            except (ValueError, ValidationError) as exc:
                from app.application.learning_counterexamples import capture_counterexample
                raise WorkflowValidationFailure(f"workflowExamples[{example_index}]：{exc}", observations,
                    capture_counterexample(example, exc)) from exc
            successful_inputs.append(inputs)
    from app.application.input_form_coverage import validate_input_form_coverage
    validate_input_form_coverage(compiled.definition.spec.input_schema, successful_inputs)



async def validate_observed_capability(version, interface_name, inputs, evidence, invocation_id):
    """Replay writes from exact captured calls; run read-only/model assertions live.

    Evidence never authorizes future calls: original confirmations remain in the locked
    interface. A new input without recorded write evidence leaves the graph a candidate.
    """
    from app.application.skillx_execution import execute_skillx
    from app.domain.workflow import NodeResult

    interface = version.manifest_json["interfaces"][interface_name]
    locks = version.manifest_json.get("dependencyLocks", {})
    captured = [
        call
        for call in evidence.values()
        if call.kind == "skillx"
        and call.status == "succeeded"
        and call.request_json.get("versionId") == version.id
        and call.request_json.get("interface") == interface_name
        and call.request_json.get("input", {}) == inputs
    ]
    if not captured:
        raise ValueError("能力组合缺少同版本、同接口、同输入的原始调用证据，保留候选")
    witness = captured[0]
    Draft202012Validator(interface["inputSchema"]).validate(inputs)
    Draft202012Validator(interface["outputSchema"]).validate(witness.output_json)
    effects = interface.get("effects") or any(
        lock.get("effect", "unknown") not in {"none", "read", "data.read"}
        for lock in locks.values()
    )
    if effects:
        return NodeResult(
            status="succeeded",
            output=witness.output_json,
            metrics={"fixtureInvocationId": witness.id},
        )
    result = await execute_skillx(
        version.id, interface_name, inputs, "validation-" + invocation_id, witness.runtime_json
    )
    if result.status != "succeeded" or result.output != witness.output_json:
        raise ValueError("能力真实执行未通过原任务业务断言，保留候选")
    return result
