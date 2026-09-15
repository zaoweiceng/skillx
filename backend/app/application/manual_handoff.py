"""Explicit handoff of failed executions, including historical versions without fallback."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.execution_sources import load_execution_source
from app.application.recovery import build_checkpoint, verify_checkpoint
from app.application.run_events import append_event
from app.application.runtime_profiles import agent_snapshot
from app.domain.serialization import digest_json
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import ExecutionCheckpoint, NodeRun, Run


async def request_handoff(
    session: AsyncSession, run_id: str, node_run_id: str, profile_id: str, reason: str
) -> ExecutionCheckpoint:
    run = await session.get(Run, run_id)
    failed = await session.get(NodeRun, node_run_id)
    if run is None or failed is None or failed.run_id != run_id:
        raise LookupError("运行或失败节点不存在")
    if run.status != "failed" or failed.status != "failed":
        raise ValueError("只允许接力已失败的运行；取消和执行中的运行不能接力")
    if (failed.error_json or {}).get("code") == "UNKNOWN_OUTCOME":
        raise ValueError("副作用结果未知，需要先核实结果")
    latest_id = await session.scalar(
        select(NodeRun.id)
        .where(NodeRun.run_id == run_id, NodeRun.node_id == failed.node_id)
        .order_by(NodeRun.attempt.desc())
        .limit(1)
    )
    if latest_id != node_run_id:
        raise ValueError("只能接力该节点的最新失败尝试")
    source = await load_execution_source(session, run)
    definition = WorkflowDefinition.model_validate(source.definition_json)
    if not any(node.id == failed.node_id for node in definition.spec.nodes):
        raise ValueError("请选择工作流中的失败节点")
    runtime = await agent_snapshot(session, profile_id)
    claimed = await session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == "failed")
        .values(status="recovering", finished_at=None)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        raise ValueError("运行状态已变化，请刷新后重试")
    checkpoint = await session.scalar(
        select(ExecutionCheckpoint).where(
            ExecutionCheckpoint.failed_node_run_id == node_run_id,
            ExecutionCheckpoint.attempt == failed.attempt,
        )
    )
    if checkpoint is None:
        checkpoint = await build_checkpoint(session, run, definition, failed)
    else:
        verify_checkpoint(checkpoint)
        if checkpoint.status in {"pending", "running", "applied"}:
            raise ValueError("该检查点正在恢复或已应用")
    previous = {
        "digest": checkpoint.digest,
        "decision": checkpoint.decision_json,
        "recoveryNodeRunId": checkpoint.recovery_node_run_id,
    }
    fallback = checkpoint.snapshot_json.get("fallbackConfig", {})
    snapshot = {
        **checkpoint.snapshot_json,
        "fallbackConfig": {
            **fallback,
            "target": fallback.get("target") or f"handoff-{failed.node_id}"[:64],
            "agentRuntimeSnapshot": runtime,
            "requireUserApproval": False,
        },
        "manualHandoff": {"agentProfileId": profile_id, "reason": reason},
    }
    checkpoint.snapshot_json, checkpoint.digest = snapshot, digest_json(snapshot)
    checkpoint.status, checkpoint.approval_status = "pending", "approved"
    checkpoint.decision_json = None
    checkpoint.lease_owner = checkpoint.lease_expires_at = checkpoint.claim_token = None
    checkpoint.recovery_node_run_id = None
    await append_event(
        session,
        run_id,
        "recovery.manual_requested",
        {
            "checkpointId": checkpoint.id,
            "agentProfileId": profile_id,
            "reason": reason,
            "previousAttempt": previous,
        },
    )
    await session.flush()
    return checkpoint
