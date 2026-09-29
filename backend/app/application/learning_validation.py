"""Execute candidate graphs against explicit examples before activation."""

import asyncio
from collections import defaultdict
from uuid import uuid4

from jsonschema import Draft202012Validator

from app.application.capabilities import resolve_package_path
from app.application.compiler import WorkflowCompiler, digest_json
from app.application.expressions import resolve_mapping
from app.infrastructure.database import CapabilityVersion
from app.runtime.execution_contracts import CancellationToken, RunContext
from app.runtime.executors import ExecutorRegistry
from app.runtime.skillx import package_digest
from app.runtime.skillx_host import execute_in_subprocess
from app.runtime.state_machine import node_readiness


async def validate_candidate(session, definition, examples, evidence, *, generated_ids=frozenset()):
    from app.application.task_learning import observed_output

    if len(examples) < 2 or len({digest_json(x.input) for x in examples}) < 2:
        raise ValueError("工作流必须提供至少两个不同输入验证")
    if not any(
        x.sourceInvocationId in evidence
        and evidence[x.sourceInvocationId].status == "succeeded"
        and observed_output(evidence[x.sourceInvocationId]) == x.expected
        for x in examples
    ):
        raise ValueError("工作流验证缺少原任务结果证据")
    from app.application.workflow_nesting import expand_workflow

    definition = await expand_workflow(session, definition)
    if any(n["type"] not in {"input", "output", "skillx", "condition", "transform"}
           for n in definition["spec"]["nodes"]):
        raise ValueError("能力组合包含尚未提供回放证据的节点，保留候选")
    compiled = WorkflowCompiler().compile(definition)
    nodes = {n.id: n for n in compiled.definition.spec.nodes}
    incoming = defaultdict(list)
    for edge in compiled.definition.spec.edges:
        incoming[edge.target].append(edge)
    for example in examples:
        Draft202012Validator(compiled.definition.spec.input_schema).validate(example.input)
        outputs, states = {}, {}
        context = RunContext(
            str(uuid4()), example.input, str(uuid4()), CancellationToken(asyncio.Event())
        )
        for node_id in compiled.topological_order:
            node = nodes[node_id]
            if node.type == "input":
                outputs[node_id], states[node_id] = example.input, "succeeded"
                continue
            readiness, _ = node_readiness(
                incoming[node_id], states, outputs, {n.id: n.on_error for n in nodes.values()}
            )
            if readiness != "ready":
                states[node_id] = "skipped"
                continue
            inputs = resolve_mapping(node.input, example.input, outputs)
            if node.type == "skillx":
                version = await session.get(CapabilityVersion, node.config["capabilityVersionId"])
                interface = version.manifest_json["interfaces"][node.config["interface"]]
                locks = version.manifest_json.get("dependencyLocks", {})
                pure = interface["kind"] == "direct" and not interface.get("effects") and not locks
                if pure:
                    package = resolve_package_path(version.package_path)
                    if package_digest(package) != version.digest:
                        raise ValueError("候选 SkillX digest 漂移")
                    async with asyncio.timeout(30):
                        result = await execute_in_subprocess(
                            package, node.config["interface"], context.node_run_id, inputs
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
                            inputs,
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
                        inputs,
                        "graph-" + str(uuid4()),
                        proof["runtime"],
                        _candidate_version=version,
                    )
                else:
                    result = await validate_observed_capability(
                        version, node.config["interface"], inputs, evidence, context.node_run_id
                    )
            else:
                result = await ExecutorRegistry().get(node.type).execute(context, node, inputs)
            if result.status != "succeeded":
                raise ValueError(f"候选节点 {node_id} 验证失败: {result.error}")
            outputs[node_id], states[node_id] = result.output, result.status
        output_id = next(n.id for n in nodes.values() if n.type == "output")
        actual = outputs.get(output_id)
        Draft202012Validator(compiled.definition.spec.output_schema).validate(actual)
        if actual != example.expected:
            raise ValueError("工作流输出不符合业务断言")


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
