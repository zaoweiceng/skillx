from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.compiler import digest_json
from app.config import get_settings
from app.domain.workflow import NON_RECOVERABLE_ERROR_CODES, WorkflowDefinition
from app.infrastructure.database import (
    ExecutionCheckpoint,
    NodeRun,
    Run,
    utcnow,
)

# 005-D：通用 ExecutionCheckpoint 与受限 TraeX 接力。
#
# 恢复只消费持久化 checkpoint（已完成输出/Artifact 引用、失败事实、能力锁、剩余子图、
# 已提交副作用），不依赖原 Worker 内存，也不重新猜测已完成步骤。已完成的非幂等副作用
# 绝不重放；无法证明结果时进入人工确认。


# RecoveryDecision 输出 Schema：Recovery Agent 必须严格匹配。
RECOVERY_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["resume_remaining", "provide_failed_output", "request_user", "abort"],
        },
        "failedOutput": {"type": "object"},
        "reason": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": True,
}


async def build_checkpoint(
    session: AsyncSession,
    run: Run,
    definition: WorkflowDefinition,
    failed_node_run: NodeRun,
) -> ExecutionCheckpoint:
    """冻结失败点的恢复上下文为持久化 ExecutionCheckpoint。

    保存：已完成节点结构化输出（+ Artifact 引用摘要）、失败节点输入/标准错误、
    从已发布 DAG 计算的 remaining 子图、已提交副作用（非幂等已完成动作）与能力锁摘要，
    以及 fallback target Agent 的受限配置（供 Recovery Agent 使用）。
    """

    nodes = {node.id: node for node in definition.spec.nodes}
    failed_node = nodes.get(failed_node_run.node_id)
    fallback = failed_node.fallback if failed_node else None
    fallback_config: dict[str, Any] = {}
    if fallback is not None:
        target = nodes.get(fallback.target)
        if target is not None:
            fallback_config = {
                "allowedCapabilities": target.config.get("allowedCapabilities", []),
                "maxSteps": target.config.get("maxSteps", 4),
                "maxTotalTokens": target.config.get("maxTotalTokens", 20000),
            }

    latest = await _latest_node_runs(session, run.id)
    completed_outputs: dict[str, Any] = {}
    committed_side_effects: list[dict[str, Any]] = []
    for node_id, node_run in latest.items():
        if node_run.status == "succeeded":
            completed_outputs[node_id] = node_run.output_json
            node = nodes.get(node_id)
            if node is not None and node.side_effect:
                committed_side_effects.append(
                    {"nodeId": node_id, "idempotent": node.idempotent}
                )

    # remaining 子图：尚未成功且非 input/output 的节点。
    remaining = [
        node.id
        for node in definition.spec.nodes
        if node.id not in completed_outputs
        and node.type.value not in ("input", "output")
    ]

    snapshot = {
        "workflowVersionId": run.workflow_version_id,
        "completedOutputs": completed_outputs,
        "committedSideEffects": committed_side_effects,
        "failedNode": {
            "nodeId": failed_node_run.node_id,
            "type": failed_node_run.node_type,
            "input": failed_node_run.input_json,
            "error": failed_node_run.error_json,
            "attempt": failed_node_run.attempt,
        },
        "remainingSubgraph": remaining,
        "fallbackConfig": fallback_config,
        "runInput": run.input_json,
    }
    checkpoint = ExecutionCheckpoint(
        id=str(uuid4()),
        run_id=run.id,
        workflow_version_id=run.workflow_version_id,
        failed_node_id=failed_node_run.node_id,
        failed_node_run_id=failed_node_run.id,
        attempt=failed_node_run.attempt,
        digest=digest_json(snapshot),
        status="pending",
        snapshot_json=snapshot,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


def build_recovery_prompt(checkpoint: ExecutionCheckpoint) -> str:
    """由平台模板 + checkpoint 摘要生成 Recovery Agent prompt。

    绝不把整个数据库或原始私有轨迹拼进 prompt；只给结构化摘要与允许的决策集合。
    """

    snapshot = checkpoint.snapshot_json
    failed = snapshot.get("failedNode", {})
    summary = {
        "failedNode": {
            "nodeId": failed.get("nodeId"),
            "type": failed.get("type"),
            "errorCode": (failed.get("error") or {}).get("code"),
        },
        "completedNodes": sorted(snapshot.get("completedOutputs", {})),
        "remainingSubgraph": snapshot.get("remainingSubgraph", []),
    }
    return (
        "你是受限恢复 Agent。已发布工作流的某节点重试耗尽，请基于以下 checkpoint 摘要"
        "决定如何恢复。只能返回 RecoveryDecision，action ∈ "
        "[resume_remaining, provide_failed_output, request_user, abort]。"
        "已完成的非幂等副作用不得重放；无法确定结果时选择 request_user。\n"
        f"checkpoint: {json.dumps(summary, ensure_ascii=False)}"
    )


class RecoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


async def run_recovery_agent(
    checkpoint: ExecutionCheckpoint,
    fallback_config: dict[str, Any],
    *,
    host: AgentHost | None = None,
) -> dict[str, Any]:
    """运行受限 Recovery Agent，返回校验过的 RecoveryDecision。

    Agent 只在 Host 边界内运行；输出必须匹配 RecoveryDecision Schema，否则 fail closed。
    """

    settings = get_settings()
    host = host or AgentHost()
    request = AgentHostRequest(
        run_id=f"recovery-{checkpoint.id}",
        prompt=build_recovery_prompt(checkpoint),
        output_schema=RECOVERY_DECISION_SCHEMA,
        working_directory=settings.artifact_root / "recovery" / checkpoint.id,
        requested_capabilities=tuple(fallback_config.get("allowedCapabilities", [])),
        max_steps=int(fallback_config.get("maxSteps", 4)),
        max_total_tokens=int(fallback_config.get("maxTotalTokens", 20000)),
        max_duration_seconds=int(fallback_config.get("maxDurationSeconds", 60)),
    )
    try:
        result = await host.execute(request)
    except AgentHostError as exc:
        raise RecoveryError(exc.code, exc.message) from exc
    decision = result.output
    action = decision.get("action")
    if action not in {"resume_remaining", "provide_failed_output", "request_user", "abort"}:
        raise RecoveryError("CONTRACT_VIOLATION", f"非法 RecoveryDecision action: {action}")
    return decision


def is_recoverable(error_code: str | None, fallback_on: list[str]) -> bool:
    """判断某错误码是否允许自动进入恢复。

    - 必须在 fallback.on 声明的错误码集合内；
    - 安全/契约/未知类错误默认不自动恢复（由 compiler 校验保证需人工审批）。
    """

    if error_code is None or error_code not in fallback_on:
        return False
    return error_code not in NON_RECOVERABLE_ERROR_CODES


async def _latest_node_runs(session: AsyncSession, run_id: str) -> dict[str, NodeRun]:
    rows = (
        await session.scalars(
            select(NodeRun)
            .where(NodeRun.run_id == run_id)
            .order_by(NodeRun.node_id, NodeRun.attempt.desc())
        )
    ).all()
    latest: dict[str, NodeRun] = {}
    for row in rows:
        latest.setdefault(row.node_id, row)
    return latest


def verify_checkpoint(checkpoint: ExecutionCheckpoint) -> None:
    """恢复前再次核验 checkpoint digest 固定内容未被篡改。"""

    expected = digest_json(checkpoint.snapshot_json)
    if expected != checkpoint.digest:
        raise RecoveryError("CHECKPOINT_DRIFT", "ExecutionCheckpoint digest 与内容不一致")


async def apply_recovery_decision(
    session: AsyncSession,
    checkpoint: ExecutionCheckpoint,
    decision: dict[str, Any],
) -> str:
    """应用 RecoveryDecision，返回运行的后续状态提示。

    - provide_failed_output：把 Agent 提供的结果作为失败节点输出，标记成功后从**剩余**
      子图继续；已成功 NodeRun 不重建，已完成非幂等副作用不重放。
    - resume_remaining：不改失败节点输出（要求下游不依赖其输出），标记失败节点为 skipped
      并从剩余子图继续。
    - request_user：转人工确认。
    - abort：终止为失败。
    """

    verify_checkpoint(checkpoint)
    action = decision["action"]
    run = await session.get(Run, checkpoint.run_id)
    if run is None:
        raise RecoveryError("RUN_NOT_FOUND", "恢复目标 Run 不存在")
    failed = await session.get(NodeRun, checkpoint.failed_node_run_id)
    checkpoint.decision_json = decision
    checkpoint.status = "applied"

    if action == "abort":
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_json = {
            "code": "RECOVERY_ABORTED",
            "message": decision.get("reason", "恢复决定终止运行"),
        }
        checkpoint.status = "aborted"
        await session.flush()
        return "failed"
    if action == "request_user":
        run.status = "waiting_approval"
        checkpoint.status = "awaiting_user"
        await session.flush()
        return "waiting_approval"
    if action == "provide_failed_output":
        failed_output = decision.get("failedOutput")
        if not isinstance(failed_output, dict) or failed is None:
            raise RecoveryError(
                "CONTRACT_VIOLATION", "provide_failed_output 需要 failedOutput 对象"
            )
        # 用 Agent 提供的结果替代失败节点输出，标记成功；已成功节点不动。
        failed.status = "succeeded"
        failed.output_json = failed_output
        failed.error_json = None
        failed.finished_at = utcnow()
    elif action == "resume_remaining":
        # 失败节点标记为 skipped：下游若不依赖其输出可继续；已成功节点不重建。
        if failed is not None:
            failed.status = "skipped"
            failed.finished_at = utcnow()
    # 让 Run 回到 running，由 Orchestrator 从剩余子图继续调度。
    run.status = "running"
    run.error_json = None
    await session.flush()
    return "running"


class RecoveryDriver:
    """驱动 recovering 状态的 Run：运行受限 Recovery Agent 并应用决定。

    与其它 Worker 循环对称：领取一个 pending checkpoint，运行 Recovery Agent，应用
    RecoveryDecision，再由 Orchestrator 从剩余子图继续。恢复只消费持久化 checkpoint。
    """

    def __init__(self, host: AgentHost | None = None) -> None:
        self._host = host

    async def poll_once(self) -> bool:
        from app.application.orchestrator import Orchestrator
        from app.infrastructure.database import SessionLocal

        async with SessionLocal() as session, session.begin():
            checkpoint = await session.scalar(
                select(ExecutionCheckpoint)
                .where(ExecutionCheckpoint.status == "pending")
                .order_by(ExecutionCheckpoint.created_at)
                .limit(1)
            )
            if checkpoint is None:
                return False
            run = await session.get(Run, checkpoint.run_id)
            if run is None or run.status != "recovering":
                checkpoint.status = "orphaned"
                return True
            checkpoint_id = checkpoint.id
            fallback_config = checkpoint.snapshot_json.get("fallbackConfig", {})

        # 运行 Recovery Agent（Host 边界内）。
        try:
            async with SessionLocal() as session:
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                decision = await run_recovery_agent(
                    checkpoint, fallback_config, host=self._host
                )
        except RecoveryError as exc:
            async with SessionLocal() as session, session.begin():
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                run = await session.get(Run, checkpoint.run_id)
                checkpoint.status = "failed"
                run.status = "failed"
                run.finished_at = utcnow()
                run.error_json = {"code": exc.code, "message": exc.message}
            return True

        async with SessionLocal() as session, session.begin():
            checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
            run = await session.get(Run, checkpoint.run_id)
            # 运行 Recovery Agent 期间可能被取消：只有 checkpoint 仍 pending 且 run 仍
            # recovering 时才应用决定，避免把已取消/已终态的 Run 拉回 running。
            if checkpoint.status != "pending" or run is None or run.status != "recovering":
                if checkpoint.status == "pending":
                    checkpoint.status = "superseded"
                return True
            next_status = await apply_recovery_decision(session, checkpoint, decision)
            run_id = checkpoint.run_id
        # 应用后从剩余子图继续调度。
        if next_status == "running":
            async with SessionLocal() as session, session.begin():
                await Orchestrator().advance_run(session, run_id)
        return True
