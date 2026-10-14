"""Goal loop. A model decides; durable workers execute and resume it."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.agent_host import AgentHost, AgentHostRequest, AgentOutputValidationError
from app.application.callable_capabilities import public_call, public_error
from app.application.chat_images import agent_images
from app.application.compiler import digest_json
from app.application.conversation_context import ConversationContext, append_exchange, load_context
from app.application.runtime_profiles import agent_snapshot, restore_agent_snapshot
from app.application.security import redact
from app.application.task_budget import now, record_activity
from app.application.task_context import (
    bounded_evidence,
    call_context,
    catalog_context,
    context_record,
    encoded_size,
    observation,
)
from app.application.task_contracts import HumanRequest, TaskDecision, task_decision_schema
from app.application.task_prompt import decision_prompt
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationTurn,
    Message,
    SessionLocal,
    utcnow,
)
from app.infrastructure.models.tasks import (
    HumanInteraction,
    Invocation,
    LearningJob,
    Task,
    TaskEvent,
)

TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}


def emit(session: AsyncSession, task_id: str, kind: str, payload: dict) -> None:
    if kind.startswith("invocation."):
        payload = dict(payload)
        if "kind" in payload and "request" in payload:
            payload["kind"], payload["request"] = public_call(payload["kind"], payload["request"])
        if "error" in payload:
            payload["error"] = public_error(payload["error"])
    session.add(TaskEvent(task_id=task_id, type=kind, payload_json=redact(payload)))


async def enqueue_task(
    session: AsyncSession, conversation: Conversation, turn_id: str, goal: str,
    runtime_snapshot: dict | None = None,
):
    task = Task(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=turn_id,
        goal=goal,
        status="queued",
        state_json={},
        output_json={},
        runtime_json=runtime_snapshot or await agent_snapshot(
            session,
            conversation.agent_profile_id,
            conversation.traex_model,
            conversation.reasoning_effort,
            conversation.model_profile_id,
            conversation.agent_model,
        ),
    )
    session.add(task)
    await session.flush()
    emit(session, task.id, "task.accepted", {"goal": goal})
    return task


async def request_human(session: AsyncSession, task: Task, request: HumanRequest):
    existing = await session.scalar(
        select(HumanInteraction).where(
            HumanInteraction.task_id == task.id,
            HumanInteraction.call_key == request.key,
        )
    )
    payload = request.model_dump()
    if existing:
        if existing.snapshot_digest != digest_json(payload):
            raise ValueError("交互 key 已被不同请求使用")
        return existing
    interaction = HumanInteraction(
        id=str(uuid4()),
        task_id=task.id,
        call_key=request.key,
        kind=request.kind,
        request_json=payload,
        snapshot_digest=digest_json(payload),
    )
    session.add(interaction)
    emit(session, task.id, "human.requested", {"interactionId": interaction.id, **payload})
    return interaction


async def answer_human(
    session: AsyncSession,
    interaction_id: str,
    *,
    revision: int,
    snapshot_digest: str,
    answer: dict,
    key: str,
):
    item = await session.get(HumanInteraction, interaction_id)
    if item is None:
        raise LookupError("交互不存在")
    task = await session.get(Task, item.task_id)
    if item.revision != revision or item.snapshot_digest != snapshot_digest:
        raise ValueError("交互快照已变化")
    if item.status == "answered":
        if item.answer_key == key and item.answer_json == answer:
            return item
        raise ValueError("交互已被回答")
    if item.status != "pending" or task.status in TERMINAL:
        raise ValueError("交互已失效")
    if item.kind == "authorization" and not item.request_json.get("approvalId"):
        invocation = await session.get(Invocation, item.request_json.get("invocationId"))
        if (
            not invocation
            or invocation.status != "waiting_user"
            or invocation.request_json != item.request_json.get("operation")
        ):
            raise ValueError("授权操作已失效")
    Draft202012Validator(item.request_json["inputSchema"]).validate(answer)
    changed = await session.execute(
        update(HumanInteraction)
        .where(
            HumanInteraction.id == item.id,
            HumanInteraction.status == "pending",
            HumanInteraction.snapshot_digest == snapshot_digest,
        )
        .values(status="answered", answer_json=redact(answer), answer_key=key)
    )
    if changed.rowcount != 1:
        raise ValueError("交互已被并发消费")
    if item.request_json.get("approvalId"):
        from app.application.human_projection import answer_workflow_interaction

        await answer_workflow_interaction(session, item, answer)
    # Don't invalidate an in-flight decision; it consumes this answer next round.
    await session.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "waiting_user")
        .values(status="queued", revision=Task.revision + 1)
    )
    emit(session, task.id, "human.answered", {"interactionId": item.id, "answer": answer})
    await session.flush()
    await session.refresh(item)
    return item


async def finish_task(session: AsyncSession, task: Task, status: str, answer: str, error=None):
    task.status = status
    task.output_json = {"text": answer}
    task.error_json = error
    task.lease_token = None
    task.lease_expires_at = None
    if task.turn_id:
        turn = await session.get(ConversationTurn, task.turn_id)
        if turn:
            from app.application.product_history import finalize_turn

            await finalize_turn(session, turn.id, status)
            if turn.assistant_message_id:
                message = await session.get(Message, turn.assistant_message_id)
                if message:
                    message.content_json = {"type": "task", "taskId": task.id, "text": answer}
    emit(session, task.id, "task." + status, {"output": task.output_json, "error": error})
    if status == "cancelled":
        await session.execute(
            update(HumanInteraction)
            .where(
                HumanInteraction.task_id == task.id,
                HumanInteraction.status == "pending",
            )
            .values(status="cancelled")
        )
    if status == "succeeded":
        calls = list(await session.scalars(select(Invocation).where(Invocation.task_id == task.id)))
        from app.application.task_learning_gate import automatic_learning_decision

        nomination, reason = automatic_learning_decision(task.state_json, calls)
        emit(session, task.id, "learning.eligibility", {
            "eligible": nomination is not None, "reason": reason,
        })
        if nomination and not await session.scalar(select(LearningJob.id).where(
            LearningJob.task_id == task.id, LearningJob.kind == "distill",
        )):
            settings = get_settings()
            session.add(LearningJob(id=str(uuid4()), task_id=task.id, kind="distill", result_json={
                "automatic": True, "nomination": nomination,
                "budget": {"maxTokens": settings.automatic_learning_max_tokens,
                           "maxDurationSeconds": settings.automatic_learning_max_seconds,
                           "startedAt": utcnow().isoformat()},
            }))


async def cancel_task(session: AsyncSession, task: Task):
    from app.application.orchestrator import Orchestrator

    calls = list(await session.scalars(select(Invocation).where(Invocation.task_id == task.id)))
    for call in calls:
        if call.status not in TERMINAL:
            if call.kind in {"code", "install"}:
                from app.application.local_execution import cancel_local

                cancel_local(call.scope, call.id)
            call.status = "cancelled"
            call.lease_token = None
            if call.run_id:
                # Use the same cancellation semantics as the manual run API.
                from app.infrastructure.database import Run

                run = await session.get(Run, call.run_id)
                if run and run.status not in TERMINAL:
                    await Orchestrator().request_cancellation(session, run.id)
    await session.execute(update(LearningJob).where(
        LearningJob.task_id == task.id, LearningJob.status.in_(["queued", "running"])
    ).values(status="cancelled", lease_token=None, lease_expires_at=None))
    await finish_task(session, task, "cancelled", "任务已取消")


async def task_catalog(session: AsyncSession) -> dict:
    from app.application.callable_capabilities import callable_catalog

    capabilities = await callable_catalog(session)
    from app.application.tool_broker import ToolAdapterRegistry

    registry = ToolAdapterRegistry()
    tools = []
    for name in get_settings().agent_host_capabilities:
        try:
            tools.append(registry.describe(name))
        except Exception:
            continue
    return {"capabilities": capabilities, "tools": tools}


class TaskDriver:
    async def poll_once(self) -> bool:
        async with SessionLocal() as session, session.begin():
            earlier = aliased(Task)
            task = await session.scalar(
                select(Task)
                .where(
                    Task.status.not_in(TERMINAL | {"manual", "learning"}),
                    # Conversation history has one writer at a time. Do not let
                    # later turns overtake a running or human-blocked turn.
                    ~select(earlier.id).where(
                        earlier.conversation_id == Task.conversation_id,
                        earlier.status.not_in(TERMINAL | {"manual", "learning"}),
                        or_(earlier.created_at < Task.created_at,
                            (earlier.created_at == Task.created_at) & (earlier.id < Task.id)),
                    ).exists(),
                    ~select(Invocation.id)
                    .where(Invocation.task_id == Task.id, Invocation.status.not_in(TERMINAL))
                    .exists(),
                    ~select(HumanInteraction.id)
                    .where(
                        HumanInteraction.task_id == Task.id, HumanInteraction.status == "pending"
                    )
                    .exists(),
                    or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < utcnow()),
                )
                .order_by(Task.updated_at)
                .limit(1)
            )
            if task is None:
                return False
            calls = list(
                await session.scalars(
                    select(Invocation).where(Invocation.task_id == task.id)
                    .order_by(Invocation.created_at, Invocation.id)
                )
            )
            questions = list(
                await session.scalars(
                    select(HumanInteraction).where(HumanInteraction.task_id == task.id)
                )
            )
            if any(x.status not in TERMINAL for x in calls):
                task.status = "waiting_execution"
                return False
            if any(x.status == "pending" for x in questions):
                task.status = "waiting_user"
                return False
            settings = get_settings()
            if (
                task.decisions >= settings.task_max_decisions
                or task.active_ms >= settings.task_max_active_seconds * 1000
                or task.total_tokens >= settings.agent_max_total_tokens
            ):
                await finish_task(
                    session,
                    task,
                    "blocked",
                    "任务预算已耗尽，未完成事项已保留",
                    {"code": "TASK_BUDGET_EXHAUSTED"},
                )
                return True
            token = str(uuid4())
            changed = await session.execute(
                update(Task)
                .where(
                    Task.id == task.id,
                    Task.revision == task.revision,
                    or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < utcnow()),
                )
                .values(
                    status="deciding",
                    lease_token=token,
                    lease_expires_at=utcnow()
                    + timedelta(seconds=settings.agent_max_duration_seconds + 30),
                    decisions=Task.decisions + 1,
                    revision=Task.revision + 1,
                )
            )
            if changed.rowcount != 1:
                return False
            task_id = task.id
            runtime = restore_agent_snapshot(task.runtime_json)
            history = (
                list(
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == task.conversation_id)
                        .where(Message.created_at <= task.created_at)
                        .order_by(Message.created_at.desc())
                        .limit(20)
                    )
                )
                if task.conversation_id
                else []
            )
            use_history = bool(task.conversation_id and runtime and runtime.type == "pi")
            saved_context = (
                await load_context(session, task.conversation_id)
                if use_history else ConversationContext([], [], [])
            )
            model_messages = saved_context.messages
            # The catalog location is conversation-scoped, not task-scoped.
            context_directory = settings.artifact_root / (
                "conversations" if use_history else "tasks"
            ) / (task.conversation_id if use_history else task_id) / "context"
            previous_records = []
            if use_history and not saved_context.exchanges:
                # Bootstrap pre-upgrade conversations from durable execution
                # records, rather than pretending UI answers are full evidence.
                previous_records = list(await session.scalars(
                    select(Invocation).join(Task, Invocation.task_id == Task.id).where(
                        Task.conversation_id == task.conversation_id,
                        Task.id != task_id, Task.created_at <= task.created_at,
                        Task.status.in_(TERMINAL), Invocation.status.in_(TERMINAL),
                    ).order_by(Invocation.created_at, Invocation.id)
                ))
            call_records = [
                {
                    "id": x.id, "key": x.call_key,
                    "taskId": x.task_id,
                    "kind": public_call(x.kind, x.request_json)[0],
                    "request": public_call(x.kind, x.request_json)[1],
                    "status": x.status, "output": x.output_json,
                    "error": public_error(x.error_json),
                }
                for x in [*previous_records, *calls]
            ]
            if settings.task_local_execution_enabled:
                observed_calls, call_history = call_context(
                    call_records, context_directory, limit=settings.task_context_max_characters,
                    fresh_keys={call["key"] for call in task.state_json.get("calls", [])},
                )
            else:
                # Do not require local file reads when code execution is disabled.
                observed_calls = [{
                    **record, "request": observation(record["request"], 2000),
                    "output": observation(record["output"], max(2000, 18000 // len(call_records))),
                } for record in call_records]
                call_history = {}
            known_records = {record["id"]: record for record in saved_context.evidence}
            new_observations = [record for record in observed_calls
                                if record["id"] not in known_records]
            known_records.update({record["id"]: record for record in new_observations})
            validation_calls = list(known_records.values()) if use_history else observed_calls
            current_call_ids = {call.id for call in calls}
            exchange_evidence = new_observations
            history_budget = min(
                settings.conversation_context_max_characters,
                max(8192, int((runtime.model_options if runtime else {}).get(
                    "contextWindow", 128000)) // 2),
            )
            from app.application.chat_images import MAX_TOTAL_IMAGE_BYTES

            historical_images = [image for message in model_messages
                                 for image in message.get("images", [])]
            historical_paths = {image["path"] for image in historical_images}
            incoming_images = [image for image in task.state_json.get("images", [])
                               if agent_images([image])[0]["path"] not in historical_paths]
            image_bytes = sum(Path(image["path"]).stat().st_size
                              for image in historical_images if Path(image["path"]).is_file())
            missing_images = any(not Path(image["path"]).is_file() for image in historical_images)
            is_checkpoint = bool(model_messages and (
                encoded_size(model_messages) > history_budget
                or len(historical_images) + len(incoming_images) > 8
                or image_bytes + sum(image.get("size", 0) for image in
                                     incoming_images) > MAX_TOTAL_IMAGE_BYTES
                or missing_images
                or (historical_images and not runtime.model_options.get("images"))
            ))
            checkpoint = None
            if is_checkpoint:
                checkpoint = {
                    "reason": "conversation_context_budget",
                    "previousExchanges": len(saved_context.exchanges),
                    "evidenceCount": len(validation_calls),
                    "historicalImages": historical_images,
                    "imageNote": ("旧图像引用保留在档案；本次仅加载 imageInputs 中的图像，"
                                  "不得假称看过未加载图片。"),
                    "recentDecisions": [bounded_evidence(exchange.response, 2000)
                                        for exchange in saved_context.exchanges[-3:]],
                    "archivePath": context_record(context_directory, {
                        "messages": model_messages, "evidence": validation_calls,
                    }) if settings.task_local_execution_enabled else None,
                }
                # Start a new cache epoch explicitly. Old rows remain immutable;
                # references retain provenance while previews share a fixed budget.
                model_messages = []
                if settings.task_local_execution_enabled:
                    observed_calls, call_history = call_context(
                        validation_calls, context_directory,
                        limit=settings.task_context_max_characters,
                    )
                else:
                    observed_calls = [
                        {**record, "output": bounded_evidence(record.get("output"), 1000)}
                        for record in validation_calls[-20:]
                    ]
                new_observations = observed_calls
                # Keep every previously observed ID valid across checkpoints.
                # Large bodies remain in the immutable archive/recordPath, while
                # selected recent records keep their previews in the new epoch.
                compact_records = {
                    record["id"]: {
                        **{key: record[key] for key in (
                            "id", "key", "taskId", "kind", "status", "recordPath",
                        ) if key in record},
                        "request": {}, "output": None, "error": None,
                    } for record in validation_calls
                }
                compact_records.update({record["id"]: record for record in observed_calls})
                exchange_evidence = list(compact_records.values())
            context = {
                "executionEnvironment": {
                    "localExecutionEnabled": settings.task_local_execution_enabled,
                    "codeLanguages": (
                        ["python", "javascript"] if settings.task_local_execution_enabled else []
                    ),
                    "commandLineViaCode": settings.task_local_execution_enabled,
                    "capabilityCatalogIsExhaustive": False,
                },
                # Stable prefix improves provider cache reuse across decisions.
                "catalog": (
                    catalog_context(
                        await task_catalog(session), context_directory, discovery_only=True,
                    )
                    if settings.task_local_execution_enabled else await task_catalog(session)
                ),
                "goal": task.goal,
                "sessionMessages": [
                    {"role": m.role, "content": observation(m.content_json, 2000)}
                    for m in reversed(history)
                ] if not model_messages else [],
                "state": {k: v for k, v in task.state_json.items() if k not in {"calls", "answer"}},
                "calls": new_observations if use_history else observed_calls,
                "callHistory": call_history,
                "humanAnswers": [
                    {"id": x.id, "request": x.request_json, "answer": x.answer_json}
                    for x in questions
                ],
            }
            if checkpoint:
                context["contextCheckpoint"] = checkpoint
            if model_messages:
                # Catalog and managed skill updates append, never rewrite history.
                previous_catalog = saved_context.exchanges[-1].request.get("catalogDigest")
                catalog_digest = digest_json(context["catalog"])
                if previous_catalog == catalog_digest:
                    del context["catalog"]
            else:
                catalog_digest = digest_json(context["catalog"])
            image_references = list(task.state_json.get("images", []))
            if runtime and runtime.type == "pi" and runtime.model_options.get("images"):
                seen_images = {item["id"] for item in image_references}
                total_image_bytes = sum(item["size"] for item in image_references)
                for message in ([] if model_messages else history):
                    if message.created_at > task.created_at:
                        continue
                    for image in message.content_json.get("images", []):
                        if not Path(agent_images([image])[0]["path"]).is_file():
                            continue
                        if (image["id"] not in seen_images and len(image_references) < 8
                                and total_image_bytes + image["size"] <= MAX_TOTAL_IMAGE_BYTES):
                            image_references.append(image)
                            seen_images.add(image["id"])
                            total_image_bytes += image["size"]
            else:
                image_references = []
            context["imageInputs"] = [
                {"id": item["id"], "name": item["name"]} for item in image_references
            ]
            request_images = agent_images(image_references)
            if model_messages:
                seen_paths = {image["path"] for message in model_messages
                              for image in message.get("images", [])}
                request_images = [image for image in request_images
                                  if image["path"] not in seen_paths]
        started = now()

        async def cancelled():
            async with SessionLocal() as session:
                current = await session.get(Task, task_id)
                return (
                    current is None or current.lease_token != token or current.status == "cancelled"
                )

        async def sink(runtime_event, audit_event):
            async with SessionLocal() as session, session.begin():
                current = await session.get(Task, task_id)
                if current and current.lease_token == token:
                    emit(session, task_id, "agent.audit", audit_event)

        result = None
        try:
            result = await AgentHost(runtime=runtime).execute(
                AgentHostRequest(
                    run_id=f"task-{task_id}-{token}",
                    capability_catalog_included=True,
                    discover_managed_skills_only=settings.task_local_execution_enabled,
                    images=request_images,
                    prompt=decision_prompt(context, initial=not model_messages),
                    messages=model_messages,
                    output_schema=task_decision_schema(
                        validation_calls, context["state"], current_call_ids=current_call_ids,
                    ),
                    working_directory=settings.artifact_root / "tasks" / task_id,
                    max_steps=1,
                    max_total_tokens=settings.agent_max_total_tokens - task.total_tokens,
                    # Only the decision stream is watched here; dispatched
                    # invocations run separately under their execution limits.
                    max_idle_seconds=120,
                    max_duration_seconds=min(
                        settings.agent_max_duration_seconds,
                        max(1, settings.task_max_active_seconds - task.active_ms // 1000),
                    ),
                ),
                event_sink=sink,
                cancellation=cancelled,
            )
            decision = TaskDecision.model_validate(result.output)
            async with SessionLocal() as session, session.begin():
                current = await session.get(Task, task_id)
                if current.lease_token != token or current.status != "deciding":
                    return True
                current.active_ms = await record_activity(
                    session, current.id, "decision:" + token, [(started, now())]
                )
                current.total_tokens += result.total_tokens
                current.state_json = {
                    **current.state_json,
                    **decision.model_dump(),
                    "formatRetries": 0,
                }
                current.state_json.pop("decisionError", None)
                current.state_json.pop("rejectedDecision", None)
                current.lease_token = None
                current.lease_expires_at = None
                emit(session, task_id, "task.decision", decision.model_dump())
                if decision.action == "complete":
                    known = {x["id"] for x in validation_calls if x["status"] == "succeeded"}
                    if not set(decision.evidence).issubset(known) or (
                        known & current_call_ids and not decision.evidence
                    ):
                        raise ValueError("完成决定缺少有效执行证据")
                    await finish_task(session, current, "succeeded", decision.answer)
                elif decision.action == "blocked":
                    await finish_task(
                        session, current, "blocked", decision.answer or decision.summary
                    )
                else:
                    from app.application.invocations import enqueue_invocation

                    for call in decision.calls:
                        await enqueue_invocation(
                            session,
                            kind=call.kind,
                            arguments=call.arguments,
                            key=call.key,
                            task=current,
                        )
                    for question in decision.questions:
                        await request_human(session, current, question)
                    current.status = "waiting_execution" if decision.calls else "waiting_user"
                if use_history:
                    # Persist only after the claim and decision have succeeded in
                    # this same transaction. Failed/stale attempts cannot pollute history.
                    await append_exchange(
                        session, current.conversation_id, task_id, "decision:" + token,
                        {**(result.input_message or {
                            "role": "user", "content": decision_prompt(
                                context, initial=not model_messages),
                            **({"images": request_images} if request_images else {}),
                        }), "catalogDigest": catalog_digest},
                        json.dumps(decision.model_dump(), ensure_ascii=False,
                                   separators=(",", ":")),
                        exchange_evidence, is_checkpoint=is_checkpoint,
                    )
        except asyncio.CancelledError:
            # Host uses this signal for a cancelled goal or a lost lease. It must
            # not terminate the worker's long-lived task claiming loop. Actual
            # worker shutdown still propagates cancellation to the supervisor.
            if asyncio.current_task().cancelling():
                # Decisions have no external tools: release an interrupted claim
                # so the replacement worker can resume without waiting out its lease.
                async with SessionLocal() as session, session.begin():
                    current = await session.get(Task, task_id)
                    if current and current.status == "deciding" and current.lease_token == token:
                        current.status = "queued"
                        current.lease_token = None
                        current.lease_expires_at = None
                        emit(session, task_id, "task.requeued", {"reason": "worker_shutdown"})
                raise
            return True
        except Exception as exc:
            async with SessionLocal() as session, session.begin():
                current = await session.get(Task, task_id)
                if current and current.lease_token == token:
                    # A rejected decision rolls back its transaction, but not
                    # the model work already billed. Account for both invalid
                    # returned decisions and failures reported by AgentHost.
                    current.total_tokens += max(
                        getattr(result, "total_tokens", 0),
                        getattr(exc, "total_tokens", 0),
                    )
                    current.active_ms = await record_activity(
                        session, current.id, "decision:" + token, [(started, now())]
                    )
                if (
                    current
                    and current.lease_token == token
                    and (
                        isinstance(exc, (ValueError, AgentOutputValidationError))
                        or getattr(exc, "code", None) == "MODEL_MALFORMED_OUTPUT"
                    )
                    and current.state_json.get("formatRetries", 0) < 2
                ):
                    current.status = "queued"
                    current.lease_token, current.lease_expires_at = None, None
                    current.state_json = {
                        **current.state_json,
                        "decisionError": str(exc),
                        "rejectedDecision": bounded_evidence(
                            getattr(exc, "candidate", None) or getattr(result, "output", None),
                            12000,
                        ),
                        "formatRetries": current.state_json.get("formatRetries", 0) + 1,
                    }
                    emit(session, task_id, "task.decision_rejected", {"message": str(exc)})
                elif current and current.lease_token == token:
                    await finish_task(
                        session,
                        current,
                        "failed",
                        str(exc),
                        {"code": getattr(exc, "code", "TASK_DECISION_FAILED"), "message": str(exc)},
                    )
        return True
