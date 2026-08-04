from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.expressions import ExpressionError, resolve_mapping
from app.domain.workflow import NodeType
from app.runtime.executors import (
    CancellationToken,
    ExecutorRegistry,
    RunContext,
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
) -> dict[str, Any]:
    """在内存中回放 patched 图直到 fail_node 及其下游，返回 replay 证据。

    - 先用 Compiler 校验结构（结构错误直接判失败）；
    - 按拓扑序执行到 fail_node 的下游，复用真实 NodeExecutor；
    - 非幂等副作用节点在 replay 中跳过真实副作用（以模拟成功输出代替），避免重放；
    - 返回 {replayRunId, passed, failedNodeStatus, outputs, sideEffectsSimulated, diagnostics}。
    """

    replay_run_id = f"replay-{uuid4()}"
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
    failed_status = "not_reached"
    passed = False

    input_node = next(
        (node for node in definition.spec.nodes if node.type == NodeType.INPUT), None
    )
    if input_node is not None:
        outputs[input_node.id] = safe_inputs

    for node_id in order:
        node = nodes[node_id]
        if node.type in (NodeType.INPUT,):
            continue
        try:
            resolved = resolve_mapping(node.input, safe_inputs, outputs)
        except ExpressionError as exc:
            if node_id == fail_node_id:
                failed_status = "failed"
            outputs[node_id] = {"error": {"code": "INVALID_INPUT", "message": str(exc)}}
            continue
        # 非幂等副作用节点：replay 中不执行真实副作用，用模拟成功输出代替。
        if node.side_effect and not node.idempotent:
            side_effects_simulated.append(node_id)
            outputs[node_id] = {"simulated": True, **resolved}
            if node_id == fail_node_id:
                failed_status = "succeeded"
            continue
        result = await _execute_node(registry, replay_run_id, node, resolved, safe_inputs)
        error_output = result.error.model_dump() if result.error else {}
        outputs[node_id] = result.output or {"error": error_output}
        if node_id == fail_node_id:
            failed_status = result.status
    # 原失败点在 replay 中不再失败即视为修复通过。
    passed = failed_status in ("succeeded",)
    return {
        "replayRunId": replay_run_id,
        "passed": passed,
        "failedNodeStatus": failed_status,
        "outputs": outputs,
        "sideEffectsSimulated": side_effects_simulated,
        "diagnostics": [item.model_dump() for item in compiled.diagnostics],
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
