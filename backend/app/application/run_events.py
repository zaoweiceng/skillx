"""Atomic event sequencing shared by scheduling, recovery and API services."""

from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.error_policy import public_error
from app.application.security import event_payload
from app.infrastructure.database import Run, RunEvent


async def append_event(
    session: AsyncSession,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    node_run_id: str | None = None,
) -> RunEvent:
    seq = await session.scalar(
        update(Run)
        .where(Run.id == run_id)
        .values(next_event_seq=Run.next_event_seq + 1)
        .returning(Run.next_event_seq)
    )
    if seq is None:
        raise LookupError("run not found")
    safe_payload = dict(payload)
    if isinstance(safe_payload.get("error"), dict):
        safe_payload["error"] = public_error(safe_payload["error"])
    event = RunEvent(
        id=str(uuid4()),
        run_id=run_id,
        node_run_id=node_run_id,
        seq=seq,
        type=event_type,
        payload_json=event_payload(safe_payload),
    )
    session.add(event)
    await session.flush()
    return event
