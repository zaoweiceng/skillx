from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from jsonschema import ValidationError
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update

from app.application.compiler import digest_json
from app.application.invocations import enqueue_invocation
from app.application.tasks import TERMINAL, answer_human, cancel_task
from app.infrastructure.database import (
    Approval,
    Message,
    SessionLocal,
    get_session,
    rfc3339,
)
from app.infrastructure.models.tasks import (
    HumanInteraction,
    Invocation,
    LearningJob,
    Task,
    TaskEvent,
)

router = APIRouter()


def human_view(item):
    return {
        "id": item.id,
        "taskId": item.task_id,
        "kind": item.kind,
        "status": item.status,
        "revision": item.revision,
        "snapshotDigest": item.snapshot_digest,
        "request": item.request_json,
        "answer": item.answer_json,
    }


def invocation_view(item):
    from app.application.callable_capabilities import public_call, public_error

    kind, arguments = public_call(item.kind, item.request_json)
    return {
        "id": item.id,
        "taskId": item.task_id,
        "kind": kind,
        "status": item.status,
        "request": arguments,
        "output": item.output_json,
        "error": public_error(item.error_json),
        "runId": item.run_id,
        "createdAt": rfc3339(item.created_at),
    }


async def invocation_result_view(session, item):
    """Attach runtime evidence only for a completed, individually requested call."""
    from app.application.execution_receipts import build_execution_receipt

    result = invocation_view(item)
    receipt = await build_execution_receipt(session, item)
    if receipt is not None:
        result["executionReceipt"] = receipt
    return result


async def task_view(session, task):
    calls = list(
        await session.scalars(
            select(Invocation).where(Invocation.task_id == task.id).order_by(Invocation.created_at)
        )
    )
    questions = list(
        await session.scalars(
            select(HumanInteraction)
            .where(HumanInteraction.task_id == task.id)
            .order_by(HumanInteraction.created_at)
        )
    )
    approvals = []
    for call in calls:
        if call.run_id:
            approvals.extend(
                {
                    "id": a.id,
                    "runId": a.run_id,
                    "snapshotDigest": a.snapshot_digest,
                    "snapshot": a.snapshot_json,
                    "authorizationDigest": digest_json(a.snapshot_json["plan"])
                    if isinstance(a.snapshot_json.get("plan"), dict)
                    else digest_json(a.snapshot_json["operationContext"])
                    if isinstance(a.snapshot_json.get("operationContext"), dict) else None,
                    "planDigest": digest_json(a.snapshot_json["plan"])
                    if isinstance(a.snapshot_json.get("plan"), dict) else None,
                    "status": a.status,
                }
                for a in await session.scalars(
                    select(Approval).where(
                        Approval.run_id == call.run_id, Approval.status == "pending"
                    )
                )
            )
    learning = list(
        await session.scalars(select(LearningJob).where(LearningJob.task_id == task.id))
    )
    decisions = list(
        await session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.type == "task.decision")
            .order_by(TaskEvent.id)
        )
    )
    reasons = {
        call["key"]: call.get("reason", "")
        for event in decisions
        for call in event.payload_json.get("calls", [])
        if "key" in call
    }
    return {
        "id": task.id,
        "status": task.status,
        "goal": task.goal,
        "state": task.state_json,
        "output": task.output_json,
        "error": task.error_json,
        "decisions": task.decisions,
        "totalTokens": task.total_tokens,
        "invocations": [
            {
                **invocation_view(x),
                "reason": reasons.get(x.call_key),
                "workingDirectory": task.state_json.get("workingDirectory"),
            }
            for x in calls
        ],
        "interactions": [human_view(x) for x in questions],
        "approvals": approvals,
        "learning": [
            {"id": x.id, "kind": x.kind, "status": x.status, "result": x.result_json}
            for x in learning
        ],
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, session=Depends(get_session)):
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task 不存在")
    return await task_view(session, task)


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, session=Depends(get_session)):
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task 不存在")
    if task.status not in TERMINAL:
        await cancel_task(session, task)
        await session.commit()
    return await task_view(session, task)


class HumanAnswer(BaseModel):
    revision: int = Field(ge=1)
    snapshotDigest: str
    idempotencyKey: str = Field(min_length=1, max_length=160)
    answer: dict[str, Any]


@router.get("/human-interactions/{interaction_id}")
async def get_interaction(interaction_id: str, session=Depends(get_session)):
    item = await session.get(HumanInteraction, interaction_id)
    if item is None:
        raise HTTPException(404, "交互不存在")
    return human_view(item)


@router.post("/human-interactions/{interaction_id}/answer")
async def answer(interaction_id: str, body: HumanAnswer, session=Depends(get_session)):
    try:
        item = await answer_human(
            session,
            interaction_id,
            revision=body.revision,
            snapshot_digest=body.snapshotDigest,
            answer=body.answer,
            key=body.idempotencyKey,
        )
        await session.commit()
        return human_view(item)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, exc.message) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/tasks/{task_id}/events")
async def events(task_id: str, after: int = 0, session=Depends(get_session)):
    if await session.get(Task, task_id) is None:
        raise HTTPException(404, "Task 不存在")
    rows = await session.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id, TaskEvent.id > after)
        .order_by(TaskEvent.id)
        .limit(500)
    )
    return [
        {"id": x.id, "type": x.type, "payload": x.payload_json, "createdAt": rfc3339(x.created_at)}
        for x in rows
    ]


@router.get("/tasks/{task_id}/events/stream")
async def stream(task_id: str, request: Request, after: int = 0):
    return await stream_events(task_id, request, after)


async def stream_events(task_id, request, after, invocation_id=None):
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, "非法事件游标") from None
    async with SessionLocal() as session:
        if await session.get(Task, task_id) is None:
            raise HTTPException(404, "Task 不存在")

    async def generate():
        nonlocal cursor
        while not await request.is_disconnected():
            async with SessionLocal() as session:
                rows = await events(task_id, cursor, session)
                task = await session.get(Task, task_id)
                target = await session.get(Invocation, invocation_id) if invocation_id else task
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
            if not rows and (target is None or target.status in TERMINAL):
                break
            if not rows:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/conversations/{conversation_id}/session-export")
async def export_session(conversation_id: str, session=Depends(get_session)):
    tasks = list(await session.scalars(select(Task).where(Task.conversation_id == conversation_id)))
    messages = list(
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
    )
    return {
        "schemaVersion": "1",
        "conversationId": conversation_id,
        "messages": [{"id": x.id, "role": x.role, "content": x.content_json} for x in messages],
        "tasks": [
            {
                **await task_view(session, task),
                "events": [
                    {"id": e.id, "type": e.type, "payload": e.payload_json}
                    for e in await session.scalars(
                        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
                    )
                ],
            }
            for task in tasks
        ],
    }


class InvocationCreate(BaseModel):
    interface: str = "execute"
    input: dict[str, Any]
    idempotencyKey: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=160)


@router.get("/capability-catalog")
async def capability_catalog(session=Depends(get_session), include_disabled: bool = False):
    from app.application.callable_capabilities import callable_catalog

    return await callable_catalog(session, include_disabled=include_disabled)


class CapabilityAvailabilityUpdate(BaseModel):
    enabled: bool = Field(strict=True)


@router.patch("/capabilities/{capability_id}/availability")
async def update_capability_availability(capability_id: str, body: CapabilityAvailabilityUpdate, session=Depends(get_session)):
    from app.infrastructure.models import Capability, Workflow, CapabilityAvailability
    if await session.get(Capability, capability_id) is None and await session.get(Workflow, capability_id) is None:
        raise HTTPException(404, "能力不存在")
    state = await session.get(CapabilityAvailability, capability_id)
    if state is None:
        state = CapabilityAvailability(capability_id=capability_id, enabled=body.enabled)
        session.add(state)
    else:
        state.enabled = body.enabled
    await session.commit()
    return {"capabilityId": capability_id, "enabled": state.enabled}


@router.get("/capability-versions/{version_id}/contract")
async def capability_contract(version_id: str, session=Depends(get_session)):
    from app.application.callable_capabilities import resolve_callable

    try:
        return (await resolve_callable(session, version_id)).contract
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/capability-versions/{version_id}/invocations", status_code=202)
async def create_invocation(version_id: str, body: InvocationCreate, session=Depends(get_session)):
    try:
        item = await enqueue_invocation(
            session,
            kind="capability",
            arguments={
                "capabilityVersionId": version_id,
                "interface": body.interface,
                "input": body.input,
            },
            key=body.idempotencyKey,
        )
        await session.commit()
        return await invocation_result_view(session, item)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/capabilities/{capability_id}/versions")
async def versions(capability_id: str, session=Depends(get_session)):
    from app.application.callable_capabilities import callable_versions

    return await callable_versions(session, capability_id)


@router.get("/invocations")
async def invocations(versionId: str | None = None, session=Depends(get_session)):
    query = select(Invocation).where(Invocation.kind.in_(["skillx", "workflow"]))
    if versionId:
        query = query.where(Invocation.request_json["versionId"].as_string() == versionId)
    rows = await session.scalars(query.order_by(Invocation.created_at.desc()).limit(100))
    result = []
    for item in rows:
        interactions = await session.scalars(
            select(HumanInteraction).where(
                or_(
                    HumanInteraction.call_key == "approve:" + item.id,
                    HumanInteraction.request_json["runId"].as_string() == item.run_id
                    if item.run_id
                    else False,
                )
            )
        )
        result.append(
            {**invocation_view(item), "interactions": [human_view(x) for x in interactions]}
        )
    return result


@router.get("/invocations/{invocation_id}")
async def invocation(invocation_id: str, session=Depends(get_session)):
    item = await session.get(Invocation, invocation_id)
    if item is None:
        raise HTTPException(404, "Invocation 不存在")
    return await invocation_result_view(session, item)


@router.post("/invocations/{invocation_id}/cancel")
async def cancel_invocation(invocation_id: str, session=Depends(get_session)):
    item = await session.get(Invocation, invocation_id)
    if item is None:
        raise HTTPException(404, "Invocation 不存在")
    if item.status not in TERMINAL:
        if item.run_id:
            from app.application.orchestrator import Orchestrator

            await Orchestrator().request_cancellation(session, item.run_id)
        if item.kind in {"code", "install"}:
            from app.application.local_execution import cancel_local

            cancel_local(item.scope, item.id)
        item.status = "cancelled"
        item.lease_token = None
        await session.execute(
            update(HumanInteraction)
            .where(
                or_(
                    HumanInteraction.call_key == "approve:" + item.id,
                    HumanInteraction.request_json["runId"].as_string() == item.run_id
                    if item.run_id
                    else False,
                ),
                HumanInteraction.status == "pending",
            )
            .values(status="cancelled")
        )
    await session.commit()
    return invocation_view(item)


@router.get("/tasks/{task_id}/session-export")
async def export_task(task_id: str, session=Depends(get_session)):
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task 不存在")
    return {
        "schemaVersion": "1",
        **await task_view(session, task),
        "events": [
            {"id": row.id, "type": row.type, "payload": row.payload_json}
            for row in await session.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id)
            )
        ],
    }


@router.get("/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: str, session=Depends(get_session)):
    import hashlib

    from app.application.local_execution import task_directory

    if await session.get(Task, task_id) is None:
        raise HTTPException(404, "Task 不存在")
    root = task_directory(task_id)
    records = []
    evidence = root / ".executions"
    for path in sorted(evidence.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
            records.append(
                {
                    "path": str(path.relative_to(root)),
                    "size": path.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    from app.infrastructure.payloads import task_payload_references

    for digest, reference in (await task_payload_references(session, task_id)).items():
        records.append(
            {
                "path": "payloads/" + digest + ".json",
                "size": reference["size"],
                "digest": "sha256:" + digest,
                "mediaType": reference["mediaType"],
            }
        )
    return records


@router.get("/tasks/{task_id}/artifacts/{path:path}")
async def download_task_artifact(task_id: str, path: str, session=Depends(get_session)):
    from fastapi.responses import FileResponse

    from app.application.local_execution import task_directory

    if await session.get(Task, task_id) is None:
        raise HTTPException(404, "Task 不存在")
    if path.startswith("payloads/"):
        from app.config import get_settings
        from app.infrastructure.payloads import MARKER, decode, task_payload_references

        references = await task_payload_references(session, task_id)
        digest = path.removeprefix("payloads/").removesuffix(".json")
        if digest not in references or path != "payloads/" + digest + ".json":
            raise HTTPException(404, "Artifact 不存在")
        decode({MARKER: references[digest]})  # Verify digest before serving persisted bytes.
        target = get_settings().artifact_root.resolve() / "payloads" / (digest + ".json")
        return FileResponse(target, filename=target.name, media_type="application/json")
    root = task_directory(task_id) / ".executions"
    target = (task_directory(task_id) / path).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise HTTPException(404, "Artifact 不存在")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.get("/invocations/{invocation_id}/events")
async def invocation_events(invocation_id: str, after: int = 0, session=Depends(get_session)):
    item = await session.get(Invocation, invocation_id)
    if item is None:
        raise HTTPException(404, "Invocation 不存在")
    return await events(item.task_id, after, session) if item.task_id else []


@router.get("/invocations/{invocation_id}/events/stream")
async def invocation_stream(invocation_id: str, request: Request, after: int = 0):
    async with SessionLocal() as session:
        item = await session.get(Invocation, invocation_id)
        if item is None or not item.task_id:
            raise HTTPException(404, "Invocation 不存在")
        task_id = item.task_id
    return await stream_events(task_id, request, after, invocation_id)


@router.get("/runs/{run_id}/human-interactions")
async def run_interactions(run_id: str, session=Depends(get_session)):
    rows = await session.scalars(
        select(HumanInteraction)
        .where(HumanInteraction.request_json["runId"].as_string() == run_id)
        .order_by(HumanInteraction.created_at)
    )
    return [human_view(item) for item in rows]
