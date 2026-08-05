from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from app.application.compiler import WorkflowCompiler, WorkflowValidationError, digest_json
from app.application.expressions import ExpressionError, resolve_mapping
from app.domain.workflow import NodeType
from app.runtime.executors import (
    CancellationToken,
    ExecutorRegistry,
    RunContext,
)
from app.runtime.state_machine import (
    fallback_only_targets,
    node_readiness,
    output_schema_errors,
)

# 006-D-03：真实 replay。在隔离、受限的内存环境用 source checkpoint 的安全输入运行
# patched 图到原失败点及必要下游。这不是静态 Compiler 校验，而是实际执行节点执行器；
# 副作用节点以只读/幂等语义模拟，不产生真实外部副作用。replay 通过后才允许创建 draft。


class ReplayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


async def run_isolated_replay(
    patched_definition: dict[str, Any],
    *,
    safe_inputs: dict[str, Any],
    fail_node_id: str,
    fixtures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """在内存中回放 patched 图直到 fail_node 及其下游，返回 replay 证据。

    - 先用 Compiler 校验结构（结构错误直接判失败）；
    - 按拓扑序执行到 fail_node 的下游，复用真实 NodeExecutor；
    - 非幂等副作用节点在 replay 中跳过真实副作用（以模拟成功输出代替），避免重放；
    - 返回 {replayRunId, passed, failedNodeStatus, outputs, sideEffectsSimulated, diagnostics}。
    """

    replay_run_id = f"replay-{uuid4()}"
    fixtures = fixtures or {}
    try:
        compiled = WorkflowCompiler().compile(patched_definition)
    except WorkflowValidationError as exc:
        return {
            "replayRunId": replay_run_id,
            "passed": False,
            "failedNodeStatus": "compile_error",
            "outputs": {},
            "sideEffectsSimulated": [],
            "diagnostics": [item.model_dump() for item in exc.diagnostics],
        }

    definition = compiled.definition
    nodes = {node.id: node for node in definition.spec.nodes}
    order = compiled.topological_order
    registry = ExecutorRegistry()
    outputs: dict[str, Any] = {}
    side_effects_simulated: list[str] = []
    statuses: dict[str, str] = {}
    selected_edges: list[dict[str, Any]] = []
    skipped_nodes: list[str] = []
    schema_diagnostics: list[dict[str, Any]] = []
    fixture_digests: dict[str, str] = {}
    failed_status = "not_reached"
    failed_code: str | None = None
    passed = False

    input_node = next(
        (node for node in definition.spec.nodes if node.type == NodeType.INPUT), None
    )
    if input_node is not None:
        outputs[input_node.id] = safe_inputs
        statuses[input_node.id] = "succeeded"
    incoming = {node_id: [] for node_id in nodes}
    for edge in definition.spec.edges:
        incoming[edge.target].append(edge)
    on_errors = {node_id: node.on_error for node_id, node in nodes.items()}
    fallback_only = fallback_only_targets(nodes)

    for node_id in order:
        node = nodes[node_id]
        if node.type in (NodeType.INPUT,):
            continue
        if node_id in fallback_only:
            statuses[node_id] = "skipped"
            skipped_nodes.append(node_id)
            continue
        readiness, selected = node_readiness(
            incoming[node_id], statuses, outputs, on_errors
        )
        if readiness == "waiting":
            statuses[node_id] = "skipped"
            skipped_nodes.append(node_id)
            continue
        if readiness == "skipped":
            statuses[node_id] = "skipped"
            skipped_nodes.append(node_id)
            continue
        selected_edges.extend(selected)
        try:
            resolved = resolve_mapping(node.input, safe_inputs, outputs)
        except ExpressionError as exc:
            if node_id == fail_node_id:
                failed_status = "failed"
            outputs[node_id] = {"error": {"code": "INVALID_INPUT", "message": str(exc)}}
            statuses[node_id] = "failed"
            continue
        external_types = {
            NodeType.MCP,
            NodeType.SKILLX,
            NodeType.BROWSER,
            NodeType.LLM,
            NodeType.AGENT,
        }
        if node.side_effect or node.type in external_types:
            fixture = fixtures.get(node_id)
            if not isinstance(fixture, dict):
                from app.domain.workflow import ExecutionError, NodeResult

                result = NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="REPLAY_UNSUPPORTED",
                        message=f"外部读取节点 {node_id} 缺少捕获 fixture",
                    ),
                )
            else:
                fixture_digests[node_id] = digest_json(fixture)
                schema = node.config.get("outputSchema")
                errors = (
                    output_schema_errors(schema, fixture)
                    if isinstance(schema, dict)
                    else []
                )
                if node.side_effect and not isinstance(schema, dict):
                    errors = ["副作用模拟缺少冻结 outputSchema"]
                if errors:
                    from app.domain.workflow import ExecutionError, NodeResult

                    schema_diagnostics.append(
                        {"nodeId": node_id, "errors": errors}
                    )
                    result = NodeResult(
                        status="failed",
                        error=ExecutionError(
                            code="REPLAY_UNSUPPORTED",
                            message=f"节点 {node_id} fixture 不满足输出契约",
                        ),
                    )
                else:
                    from app.domain.workflow import NodeResult

                    result = NodeResult(status="succeeded", output=fixture)
                    if node.side_effect:
                        side_effects_simulated.append(node_id)
        else:
            result = await _execute_node(
                registry, replay_run_id, node, resolved, safe_inputs
            )
        error_output = result.error.model_dump() if result.error else {}
        outputs[node_id] = result.output or {"error": error_output}
        statuses[node_id] = result.status
        if node_id == fail_node_id:
            failed_status = result.status
            failed_code = result.error.code if result.error else None
    output_node = next(
        (node for node in definition.spec.nodes if node.type == NodeType.OUTPUT), None
    )
    final_status = statuses.get(output_node.id, "not_reached") if output_node else "not_reached"
    final_output = outputs.get(output_node.id, {}) if output_node else {}
    final_schema_errors = output_schema_errors(
        definition.spec.output_schema, final_output
    )
    if final_schema_errors:
        schema_diagnostics.append(
            {"nodeId": output_node.id if output_node else "output", "errors": final_schema_errors}
        )
    passed = (
        failed_status == "succeeded"
        and final_status == "succeeded"
        and not final_schema_errors
    )
    return {
        "replayRunId": replay_run_id,
        "passed": passed,
        "failedNodeStatus": failed_status,
        "failedNodeErrorCode": failed_code,
        "finalOutputStatus": final_status,
        "outputs": outputs,
        "sideEffectsSimulated": side_effects_simulated,
        "selectedEdges": selected_edges,
        "skippedNodes": skipped_nodes,
        "schemaDiagnostics": schema_diagnostics,
        "fixtureDigests": fixture_digests,
        "diagnostics": [item.model_dump() for item in compiled.diagnostics],
    }


async def run_replay_proof(
    base_definition: dict[str, Any],
    patched_definition: dict[str, Any],
    *,
    safe_inputs: dict[str, Any],
    fail_node_id: str,
    expected_error_code: str,
    fixtures: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    baseline = await run_isolated_replay(
        base_definition,
        safe_inputs=safe_inputs,
        fail_node_id=fail_node_id,
        fixtures=fixtures,
    )
    reproduced = (
        baseline["failedNodeStatus"] == "failed"
        and baseline["failedNodeErrorCode"] == expected_error_code
    )
    if not reproduced:
        return {
            "passed": False,
            "baselineReproduced": False,
            "baseline": baseline,
            "patched": None,
            "diff": {
                "expectedErrorCode": expected_error_code,
                "actualErrorCode": baseline["failedNodeErrorCode"],
            },
        }
    patched = await run_isolated_replay(
        patched_definition,
        safe_inputs=safe_inputs,
        fail_node_id=fail_node_id,
        fixtures=fixtures,
    )
    return {
        "passed": patched["passed"],
        "baselineReproduced": True,
        "baseline": baseline,
        "patched": patched,
        "diff": {
            "failedNode": fail_node_id,
            "before": baseline["failedNodeStatus"],
            "after": patched["failedNodeStatus"],
            "finalOutputStatus": patched["finalOutputStatus"],
        },
    }


async def _execute_node(
    registry: ExecutorRegistry,
    replay_run_id: str,
    node: Any,
    resolved: dict[str, Any],
    safe_inputs: dict[str, Any],
) -> Any:
    executor = registry.get(node.type)
    context = RunContext(
        run_id=replay_run_id,
        run_input=safe_inputs,
        node_run_id=f"{replay_run_id}:{node.id}",
        cancellation=CancellationToken(asyncio.Event()),
    )
    try:
        return await asyncio.wait_for(
            executor.execute(context, node, resolved), timeout=node.timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 - replay 中任何执行异常都记为失败结果，不击穿。
        from app.domain.workflow import ExecutionError, NodeResult

        return NodeResult(
            status="failed",
            error=ExecutionError(code="REPLAY_EXECUTOR_ERROR", message=str(exc)),
        )


def replay_definition(
    patched_definition: dict[str, Any],
    *,
    safe_inputs: dict[str, Any],
    fail_node_id: str,
) -> dict[str, Any]:
    """同步封装，便于在非 async 上下文（如路由已在事务外）调用。"""

    return asyncio.run(
        run_isolated_replay(
            patched_definition, safe_inputs=safe_inputs, fail_node_id=fail_node_id
        )
    )
