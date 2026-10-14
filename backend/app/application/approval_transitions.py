"""Approval transitions shared by legacy endpoints and persistent human cards.

The caller owns the transaction; answering the card and resuming its run are atomic.
"""
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import Decision, RecoveryAnswer
from app.application.compiler import digest_json
from app.application.orchestrator import Orchestrator
from app.application.run_events import append_event
from app.application.security import redact
from app.infrastructure.database import Approval, ExecutionCheckpoint, NodeRun, Run, utcnow

orchestrator = Orchestrator()

async def _approve_recovery(session: AsyncSession, approval: Approval) -> dict[str, Any]:
    """批准恢复：checkpoint→approved 且 Run→recovering，交给同一 Recovery claim。"""

    checkpoint_id = approval.snapshot_json.get("checkpointId")
    checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
    if checkpoint is None or checkpoint.status != "pending":
        raise HTTPException(409, "恢复检查点不可用或已处理")
    run = await session.get(Run, approval.run_id)
    if run is None or run.status != "waiting_approval":
        raise HTTPException(409, "运行不在等待恢复审批状态")
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    checkpoint.approval_status = "approved"
    Orchestrator._set_wait_clock(run, False)
    run.status = "recovering"
    await append_event(
        session,
        approval.run_id,
        "run.recovery_requested",
        {"runId": run.id, "checkpointId": checkpoint.id, "failedNode": approval.node_id},
        approval.node_run_id,
    )
    await session.flush()
    return {"id": approval.id, "status": approval.status}


async def approve(
    approval_id: str,
    body: Decision,
    session: AsyncSession,
    *,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if approval.status != "pending":
        raise HTTPException(409, "审批已处理")
    if approval.snapshot_digest != body.snapshot_digest:
        raise HTTPException(
            409,
            detail={"code": "APPROVAL_SNAPSHOT_CHANGED", "message": "审批快照已变化"},
        )
    if digest_json(approval.snapshot_json) != approval.snapshot_digest:
        raise HTTPException(
            409,
            detail={"code": "APPROVAL_SNAPSHOT_CHANGED", "message": "服务端审批快照已变化"},
        )
    effect = approval.snapshot_json.get("effect")
    # 恢复审批：批准后 checkpoint→approved 且 Run→recovering，由同一 Recovery claim 处理；
    # 关联的失败 NodeRun 保持 failed（不可变）。
    if effect == "recovery.approval":
        return await _approve_recovery(session, approval)
    node_run = await session.get(NodeRun, approval.node_run_id)
    if node_run is None or node_run.status != "waiting_user":
        raise HTTPException(409, "关联节点不在等待状态")
    run = await session.get(Run, approval.run_id)
    if not run or run.cancellation_requested_at or run.status in {"failed", "cancelled", "succeeded"}:
        raise HTTPException(409, "运行已结束或取消")
    if effect == "workflow.run":
        from app.application.run_authorization import approval_plan, covers
        plan = approval.snapshot_json.get("plan")
        if (
            not isinstance(plan, dict)
            or plan != await approval_plan(session, run.id)
            or not covers(plan, node_run.node_id, approval.snapshot_json.get("operation"),
                          approval.snapshot_json.get("versionDigest"))
        ):
            raise HTTPException(409, "工作流执行计划已变化")
    if effect == "skillx.invoke" and "operationContext" in approval.snapshot_json:
        from app.application.run_authorization import operation_context
        if approval.snapshot_json["operationContext"] != await operation_context(
            session, run.id, node_run.id, approval.snapshot_json.get("operation", {})
        ):
            raise HTTPException(409, "当前操作输入或上游执行证据已变化")
    approval.status = "approved"
    approval.decided_by = "dsh-host-policy" if provenance else "local-user"
    approval.decided_at = utcnow()
    if node_run.node_type == "browser" and effect == "form.submit":
        authorization_digest_value = approval.snapshot_json.get("authorizationDigest")
        if not authorization_digest_value:
            raise HTTPException(409, "浏览器审批缺少服务端授权摘要")
        node_run.status = "ready"
        node_run.input_json = {
            **node_run.input_json,
            "_authorizationDigest": authorization_digest_value,
        }
        node_run.output_json = None
        node_run.error_json = None
        node_run.available_at = utcnow()
    elif node_run.node_type == "skillx" and effect in {"skillx.invoke", "workflow.run"}:
        node_run.status = "ready"
        node_run.output_json = None
        node_run.error_json = None
        node_run.available_at = utcnow()
    elif node_run.node_type == "browser" and effect == "browser.drift":
        drift_authorization_value = approval.snapshot_json.get("driftAuthorization")
        if not drift_authorization_value:
            raise HTTPException(409, "浏览器审批缺少服务端漂移授权摘要")
        node_run.status = "ready"
        node_run.input_json = {
            **node_run.input_json,
            "_driftAuthorization": drift_authorization_value,
        }
        node_run.output_json = None
        node_run.error_json = None
        node_run.available_at = utcnow()
    elif (
        node_run.node_type == "browser"
        and (node_run.error_json or {}).get("code") == "UNKNOWN_OUTCOME"
    ):
        node_run.status = "failed"
        node_run.finished_at = utcnow()
    else:
        node_run.status = "succeeded"
        node_run.output_json = {
            "approved": True,
            "snapshot": approval.snapshot_json.get("payload", {}),
        }
        node_run.finished_at = utcnow()
    await append_event(
        session,
        approval.run_id,
        "approval.resolved",
        {"approvalId": approval.id, "nodeId": approval.node_id, "status": "approved",
         **({"authorization": provenance} if provenance else {})},
        node_run.id,
    )
    await orchestrator.advance_run(session, approval.run_id)
    await session.flush()
    return {"id": approval.id, "status": approval.status}


async def answer_recovery_question(
    approval_id: str,
    body: RecoveryAnswer,
    session: AsyncSession,
) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if (
        approval.status != "pending"
        or approval.snapshot_json.get("effect") != "recovery.question"
    ):
        raise HTTPException(409, "恢复问题不可回答或已处理")
    if approval.snapshot_digest != body.snapshot_digest:
        raise HTTPException(409, "恢复问题快照已变化")
    checkpoint = await session.get(
        ExecutionCheckpoint, approval.snapshot_json.get("checkpointId")
    )
    run = await session.get(Run, approval.run_id)
    if checkpoint is None or run is None or checkpoint.status != "awaiting_user":
        raise HTTPException(409, "恢复检查点不可继续")
    checkpoint.snapshot_json = {
        **checkpoint.snapshot_json,
        "userAnswer": redact(body.answer),
    }
    checkpoint.digest = digest_json(checkpoint.snapshot_json)
    checkpoint.status = "pending"
    checkpoint.decision_json = {
        **(checkpoint.decision_json or {}),
        "userAnswer": redact(body.answer),
    }
    checkpoint.lease_owner = None
    checkpoint.lease_expires_at = None
    Orchestrator._set_wait_clock(run, False)
    run.status = "recovering"
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    await append_event(
        session,
        run.id,
        "recovery.user_answered",
        {"checkpointId": checkpoint.id, "approvalId": approval.id},
    )
    await session.flush()
    return {"id": approval.id, "status": approval.status, "checkpointId": checkpoint.id}


async def reject(approval_id: str, session: AsyncSession) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if approval.status != "pending":
        raise HTTPException(409, "审批已处理")
    # 恢复审批被拒绝：checkpoint→rejected，Run 保持原失败终态，失败历史不变。
    if approval.snapshot_json.get("effect") == "recovery.approval":
        checkpoint_id = approval.snapshot_json.get("checkpointId")
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        approval.status = "rejected"
        approval.decided_by = "local-user"
        approval.decided_at = utcnow()
        run = await session.get(Run, approval.run_id)
        failed_node = await session.get(NodeRun, approval.node_run_id)
        if checkpoint is not None:
            checkpoint.approval_status = "rejected"
            checkpoint.status = "rejected"
        if run is not None:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_json = failed_node.error_json if failed_node else {
                "code": "RECOVERY_REJECTED",
                "message": "恢复审批被拒绝",
            }
        await append_event(
            session,
            approval.run_id,
            "run.recovery_rejected",
            {"approvalId": approval.id, "checkpointId": checkpoint_id},
            approval.node_run_id,
        )
        await session.flush()
        return {"id": approval.id, "status": approval.status}
    node_run = await session.get(NodeRun, approval.node_run_id)
    approval.status = "rejected"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    if node_run:
        node_run.status = "failed"
        node_run.error_json = {
            "code": "CONFIRMATION_DENIED",
            "message": "用户拒绝审批",
            "retryable": False,
        }
        node_run.finished_at = utcnow()
    await append_event(
        session,
        approval.run_id,
        "approval.resolved",
        {"approvalId": approval.id, "nodeId": approval.node_id, "status": "rejected"},
        approval.node_run_id,
    )
    await orchestrator.advance_run(session, approval.run_id)
    await session.flush()
    return {"id": approval.id, "status": approval.status}


