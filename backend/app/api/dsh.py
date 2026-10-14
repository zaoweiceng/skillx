"""DSH plugin's explicit, user-triggered extraction boundary."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import Decision
from app.api.tasks import HumanAnswer
from app.application.dsh_extraction import DshExtractionCreate, start_dsh_extraction
from app.infrastructure.database import get_session

router = APIRouter()


@router.post("/dsh/extractions", status_code=202)
async def create_dsh_extraction(body: DshExtractionCreate, session: AsyncSession = Depends(get_session)):
    return await start_dsh_extraction(session, body)


async def bound_approval(session, invocation_id, approval_id, body):
    """Bind a host's one-time decision to the exact invocation and snapshot shown."""
    from app.application.compiler import digest_json
    from app.infrastructure.database import Approval
    from app.infrastructure.models.tasks import Invocation

    invocation = await session.get(Invocation, invocation_id)
    approval = await session.get(Approval, approval_id)
    if invocation is None or approval is None:
        raise HTTPException(404, "调用或审批不存在")
    if not invocation.run_id or invocation.run_id != approval.run_id:
        raise HTTPException(409, "审批不属于本次调用")
    if invocation.status in {"succeeded", "failed", "cancelled", "blocked"}:
        raise HTTPException(409, "调用已结束")
    if (approval.status != "pending" or body.snapshot_digest != approval.snapshot_digest
            or digest_json(approval.snapshot_json) != approval.snapshot_digest):
        raise HTTPException(409, "审批已处理或快照已变化")
    return approval


@router.post("/dsh/invocations/{invocation_id}/approvals/{approval_id}/approve")
async def approve_invocation(invocation_id: str, approval_id: str, body: Decision,
                             session: AsyncSession = Depends(get_session)):
    from app.application.approval_transitions import approve

    await bound_approval(session, invocation_id, approval_id, body)
    result = await approve(approval_id, body, session)
    await session.commit()
    return result


@router.post("/dsh/invocations/{invocation_id}/approvals/{approval_id}/reject")
async def reject_invocation(invocation_id: str, approval_id: str, body: Decision,
                            session: AsyncSession = Depends(get_session)):
    from app.application.approval_transitions import reject

    await bound_approval(session, invocation_id, approval_id, body)
    result = await reject(approval_id, session)
    await session.commit()
    return result


@router.post("/dsh/invocations/{invocation_id}/authorizations/{interaction_id}/answer")
async def answer_invocation(invocation_id: str, interaction_id: str, body: HumanAnswer,
                            session: AsyncSession = Depends(get_session)):
    from app.api.tasks import answer
    from app.infrastructure.models.tasks import HumanInteraction, Invocation

    invocation = await session.get(Invocation, invocation_id)
    item = await session.get(HumanInteraction, interaction_id)
    if invocation is None or item is None:
        raise HTTPException(404, "调用或授权不存在")
    if (item.kind != "authorization" or item.task_id != invocation.task_id
            or item.request_json.get("invocationId") != invocation.id
            or item.request_json.get("approvalId")):
        raise HTTPException(409, "授权不属于本次调用")
    return await answer(interaction_id, body, session)
