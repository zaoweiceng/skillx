from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.manual_handoff import request_handoff
from app.infrastructure.database import get_session

router = APIRouter(tags=["Recovery"])


class HandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodeRunId: str
    agentProfileId: str
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/runs/{run_id}/handoff", status_code=202)
async def handoff(run_id: str, body: HandoffInput, session: AsyncSession = Depends(get_session)):
    try:
        checkpoint = await request_handoff(
            session, run_id, body.nodeRunId, body.agentProfileId, body.reason
        )
        await session.commit()
        return {"checkpointId": checkpoint.id, "status": "recovering"}
    except (ValueError, LookupError) as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
