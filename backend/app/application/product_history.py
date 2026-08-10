from __future__ import annotations

import json
import string
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.application.error_policy import persisted_error, public_error
from app.application.orchestrator import append_event
from app.infrastructure.database import (
    ConversationRun,
    ConversationTurn,
    ModelUsage,
    Run,
    RunPresentation,
    SessionLocal,
    WorkflowVersion,
    rfc3339,
    utcnow,
)
from app.runtime.models import OpenAICompatibleAdapter

_FORMATTER = string.Formatter()
_MAX_PRESENTATION_TEMPLATE_LENGTH = 8000


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def elapsed_ms(start: datetime, end: datetime) -> int:
    return max(0, int((_aware(end) - _aware(start)).total_seconds() * 1000))


async def record_model_usage(
    session: AsyncSession,
    *,
    call_id: str,
    stage: str,
    attempt: int = 1,
    turn_id: str | None = None,
    run_id: str | None = None,
    conversation_run_id: str | None = None,
    presentation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    total_tokens: int | None = None,
    usage_complete: bool = False,
) -> None:
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = (
            input_tokens
            + output_tokens
            + (cached_tokens or 0)
            + (reasoning_tokens or 0)
        )
    await session.execute(
        sqlite_insert(ModelUsage)
        .values(
            id=str(uuid4()),
            turn_id=turn_id,
            run_id=run_id,
            conversation_run_id=conversation_run_id,
            presentation_id=presentation_id,
            call_id=call_id,
            attempt=attempt,
            provider=provider,
            model=model,
            stage=stage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            usage_complete=usage_complete,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["call_id", "attempt"])
    )


async def usage_summary(session: AsyncSession, turn_id: str) -> dict[str, Any]:
    rows = (
        await session.scalars(
            select(ModelUsage)
            .where(ModelUsage.turn_id == turn_id)
            .order_by(ModelUsage.created_at, ModelUsage.id)
        )
    ).all()
    if not rows:
        turn = await session.get(ConversationTurn, turn_id)
        if turn is not None and turn.completed_at is None:
            return {"state": "pending", "totalTokens": None, "stages": []}
        agent_run = await session.scalar(
            select(ConversationRun.id).where(ConversationRun.turn_id == turn_id).limit(1)
        )
        workflow_runs = (
            await session.scalars(select(Run).where(Run.turn_id == turn_id))
        ).all()
        may_have_called_model = agent_run is not None or (
            turn is not None and turn.status == "error"
        )
        for run in workflow_runs:
            version = (
                await session.get(WorkflowVersion, run.workflow_version_id)
                if run.workflow_version_id
                else None
            )
            if (
                version is None
                or version.validation_json.get("executionClass") != "zero_llm"
                or (
                    presentation_policy(version)["enabled"]
                    and presentation_policy(version)["producer"] == "llm"
                )
            ):
                may_have_called_model = True
        if may_have_called_model:
            return {"state": "unavailable", "totalTokens": None, "stages": []}
        return {"state": "zero", "totalTokens": 0, "stages": []}
    counted = sum(item.total_tokens or 0 for item in rows if item.total_tokens is not None)
    known = [item for item in rows if item.total_tokens is not None]
    if not known:
        state = "unavailable"
    elif all(item.usage_complete for item in rows):
        state = "complete"
    else:
        state = "partial"
    return {
        "state": state,
        "totalTokens": counted if known else None,
        "stages": [
            {
                "stage": item.stage,
                "provider": item.provider,
                "model": item.model,
                "totalTokens": item.total_tokens,
                "complete": item.usage_complete,
            }
            for item in rows
        ],
    }


async def finalize_turn(
    session: AsyncSession,
    turn_id: str | None,
    status: str,
    *,
    completed_at: datetime | None = None,
) -> None:
    if not turn_id:
        return
    turn = await session.get(ConversationTurn, turn_id)
    if turn is None or turn.completed_at is not None:
        return
    now = completed_at or utcnow()
    turn.status = status
    turn.completed_at = now
    turn.elapsed_ms = elapsed_ms(turn.accepted_at, now)


async def turn_view(session: AsyncSession, turn: ConversationTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "workflowModeSnapshot": turn.workflow_mode_snapshot,
        "status": turn.status,
        "acceptedAt": rfc3339(turn.accepted_at),
        "firstResponseAt": rfc3339(turn.first_response_at),
        "completedAt": rfc3339(turn.completed_at),
        "elapsedMs": turn.elapsed_ms,
        "usage": await usage_summary(session, turn.id),
    }


def presentation_policy(version: WorkflowVersion | None) -> dict[str, Any]:
    raw = dict(version.presentation_policy_json or {}) if version else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "audience": raw.get("audience", "internal"),
        "producer": raw.get("producer", "template"),
        "template": raw.get("template", "执行结果：{json}"),
        "prompt": raw.get(
            "prompt",
            "请把以下规范 JSON 整理为简洁、准确的中文说明，不得增加 JSON 中没有的事实：",
        ),
        "language": raw.get("language", "zh-CN"),
        "maxLength": min(8000, max(64, int(raw.get("maxLength", 2000)))),
    }


def normalize_presentation_policy(
    metadata: dict[str, Any],
    *,
    execution_class: str,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = metadata.get("presentationPolicy", {})
    if not isinstance(raw, dict):
        raise ValueError("metadata.presentationPolicy 必须是对象")
    enabled = bool(raw.get("enabled", False))
    producer = str(raw.get("producer", "template"))
    audience = str(raw.get("audience", "internal"))
    if producer not in {"template", "llm"}:
        raise ValueError("presentationPolicy.producer 仅支持 template 或 llm")
    if audience not in {"internal", "end_user"}:
        raise ValueError("presentationPolicy.audience 仅支持 internal 或 end_user")
    if enabled and audience != "end_user":
        raise ValueError("只允许为 end_user audience 启用 Presentation")
    template = str(raw.get("template", "执行结果：{json}"))
    if len(template) > _MAX_PRESENTATION_TEMPLATE_LENGTH:
        raise ValueError(
            "metadata.presentationPolicy.template 最长为 "
            f"{_MAX_PRESENTATION_TEMPLATE_LENGTH} 个字符"
        )
    if enabled and producer == "template":
        validate_presentation_template(template, output_schema or {})
    if producer == "llm" and execution_class == "zero_llm" and enabled:
        # Presentation 本身是显式、冻结的模型调用，因此发布后的执行分类不再是 zero_llm。
        execution_class = "hybrid"
    return {
        "enabled": enabled,
        "audience": audience,
        "producer": producer,
        "template": template,
        "prompt": str(
            raw.get(
                "prompt",
                "请把以下规范 JSON 整理为简洁、准确的中文说明，不得增加 JSON 中没有的事实：",
            )
        )[:8000],
        "language": str(raw.get("language", "zh-CN"))[:20],
        "maxLength": min(8000, max(64, int(raw.get("maxLength", 2000)))),
        "policyDigest": digest_json(raw),
        "executionClassWithPresentation": execution_class,
    }


def validate_presentation_template(
    template: str,
    output_schema: dict[str, Any],
) -> tuple[str, ...]:
    """Validate placeholders and return insertable top-level output fields."""

    properties = output_schema.get("properties", {})
    properties = properties if isinstance(properties, dict) else {}
    allowed = {"json", *properties}
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise ValueError(
            f"metadata.presentationPolicy.template 花括号无效: {exc}"
        ) from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name or field_name not in allowed:
            raise ValueError(
                "metadata.presentationPolicy.template 包含未知占位符 "
                f"{{{field_name}}}；仅允许 {{json}} 或 Output Schema 顶层字段"
            )
        if format_spec or conversion:
            raise ValueError(
                "metadata.presentationPolicy.template 不支持格式说明或转换: "
                f"{{{field_name}}}"
            )
        field_schema = properties.get(field_name)
        if (
            field_name != "json"
            and isinstance(field_schema, dict)
            and field_schema.get("type") in {"object", "array"}
        ):
            raise ValueError(
                "metadata.presentationPolicy.template 不能直接格式化对象或数组字段 "
                f"{{{field_name}}}；请使用 {{json}}"
            )
    return tuple(properties)


def render_presentation_template(
    template: str,
    output: dict[str, Any],
) -> str:
    """Render a prevalidated template and reject any runtime contract drift."""

    values = {
        "json": json.dumps(output, ensure_ascii=False, sort_keys=True),
        **output,
    }
    try:
        text = _FORMATTER.vformat(template, (), values)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Presentation 模板运行时字段不匹配: {exc}") from exc
    return text


def presentation_view(item: RunPresentation) -> dict[str, Any]:
    return {
        "id": item.id,
        "revision": item.revision,
        "status": item.status,
        "text": item.text,
        "producer": item.producer,
        "model": item.model,
        "error": public_error(item.error_json),
        "startedAt": rfc3339(item.started_at),
        "completedAt": rfc3339(item.completed_at),
        "elapsedMs": item.elapsed_ms,
    }


class PresentationDriver:
    async def poll_once(self) -> bool:
        async with SessionLocal() as session, session.begin():
            candidate = await session.scalar(
                select(Run)
                .join(
                    WorkflowVersion,
                    WorkflowVersion.id == Run.workflow_version_id,
                )
                .where(Run.status == "succeeded", Run.workflow_version_id.is_not(None))
                .where(
                    func.json_extract(
                        WorkflowVersion.presentation_policy_json,
                        "$.enabled",
                    )
                    == 1
                )
                .where(
                    ~select(RunPresentation.id)
                    .where(RunPresentation.run_id == Run.id)
                    .exists()
                )
                .order_by(Run.finished_at, Run.id)
                .limit(1)
            )
            if candidate is None:
                return False
            version = await session.get(WorkflowVersion, candidate.workflow_version_id)
            policy = presentation_policy(version)
            now = utcnow()
            item = RunPresentation(
                id=str(uuid4()),
                run_id=candidate.id,
                revision=1,
                status="running",
                producer=policy["producer"],
                config_digest=digest_json(policy),
                prompt_template_digest=digest_json(
                    policy["template"]
                    if policy["producer"] == "template"
                    else policy["prompt"]
                ),
                started_at=now,
            )
            session.add(item)
            await session.flush()
            await append_event(
                session,
                candidate.id,
                "presentation.started",
                {"presentationId": item.id, "producer": item.producer},
            )
            run_id = candidate.id
            presentation_id = item.id

        await self._produce(run_id, presentation_id)
        return True

    async def _produce(self, run_id: str, presentation_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(Run, run_id)
            item = await session.get(RunPresentation, presentation_id)
            version = (
                await session.get(WorkflowVersion, run.workflow_version_id)
                if run and run.workflow_version_id
                else None
            )
            if run is None or item is None:
                return
            policy = presentation_policy(version)
            output = run.output_json or {}
            try:
                if policy["producer"] == "template":
                    output_schema = (
                        version.definition_json.get("spec", {}).get("outputSchema", {})
                        if version
                        else {}
                    )
                    validate_presentation_template(
                        str(policy["template"]),
                        output_schema,
                    )
                    text = render_presentation_template(
                        str(policy["template"]),
                        output,
                    )[: policy["maxLength"]]
                    model = None
                    usage: tuple[int | None, int | None, bool] = (None, None, True)
                elif policy["producer"] == "llm":
                    result = await OpenAICompatibleAdapter().generate(
                        f"{policy['prompt']}\nJSON={json.dumps(output, ensure_ascii=False)}",
                        {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                        policy["maxLength"],
                    )
                    text = str(result.output["text"])[: policy["maxLength"]]
                    model = result.model
                    usage = (
                        result.input_tokens,
                        result.output_tokens,
                        result.usage_complete,
                    )
                else:
                    raise ValueError(f"未知 Presentation producer: {policy['producer']}")
            except Exception as exc:  # noqa: BLE001 - presentation failure is isolated.
                async with session.begin():
                    item = await session.get(RunPresentation, presentation_id)
                    run = await session.get(Run, run_id)
                    if item is None or run is None or item.status != "running":
                        return
                    now = utcnow()
                    item.status = "failed"
                    item.error_json = persisted_error(
                        {
                            "code": "PRESENTATION_FAILED",
                            "message": str(exc),
                            "retryable": False,
                        },
                        technical_detail_ref=f"presentation:{item.id}",
                    )
                    item.completed_at = now
                    item.elapsed_ms = elapsed_ms(item.started_at or now, now)
                    await append_event(
                        session,
                        run.id,
                        "presentation.failed",
                        {"presentationId": item.id, "error": item.error_json},
                    )
                    await finalize_turn(session, run.turn_id, "succeeded", completed_at=now)
                return

        chunk_size = 160
        for offset in range(0, len(text), chunk_size):
            async with SessionLocal() as session, session.begin():
                item = await session.get(RunPresentation, presentation_id)
                run = await session.get(Run, run_id)
                if item is None or run is None or item.status != "running":
                    return
                await append_event(
                    session,
                    run.id,
                    "presentation.delta",
                    {
                        "presentationId": item.id,
                        "delta": text[offset : offset + chunk_size],
                    },
                )

        async with SessionLocal() as session, session.begin():
            item = await session.get(RunPresentation, presentation_id)
            run = await session.get(Run, run_id)
            if item is None or run is None or item.status != "running":
                return
            now = utcnow()
            item.status = "completed"
            item.text = text
            item.model = model
            item.completed_at = now
            item.elapsed_ms = elapsed_ms(item.started_at or now, now)
            await append_event(
                session,
                run.id,
                "presentation.completed",
                {"presentationId": item.id, "text": text},
            )
            if item.producer == "llm":
                input_tokens, output_tokens, complete = usage
                await record_model_usage(
                    session,
                    call_id=f"presentation:{item.id}",
                    stage="presentation",
                    turn_id=run.turn_id,
                    run_id=run.id,
                    presentation_id=item.id,
                    provider="openai-compatible",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage_complete=complete,
                )
            await finalize_turn(session, run.turn_id, "succeeded", completed_at=now)


async def finalize_ready_turns(session: AsyncSession) -> int:
    turns = (
        await session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.completed_at.is_(None))
            .order_by(ConversationTurn.accepted_at)
            .limit(100)
        )
    ).all()
    completed = 0
    for turn in turns:
        workflow_runs = (
            await session.scalars(select(Run).where(Run.turn_id == turn.id))
        ).all()
        agent_runs = (
            await session.scalars(
                select(ConversationRun).where(ConversationRun.turn_id == turn.id)
            )
        ).all()
        if not workflow_runs and not agent_runs:
            if turn.status in {"ask_user", "error", "cancelled"}:
                await finalize_turn(session, turn.id, turn.status)
                completed += 1
            continue
        all_runs: list[Run | ConversationRun] = [*workflow_runs, *agent_runs]
        if any(item.status not in {"succeeded", "failed", "cancelled"} for item in all_runs):
            continue
        if workflow_runs:
            waiting = False
            for run in workflow_runs:
                version = (
                    await session.get(WorkflowVersion, run.workflow_version_id)
                    if run.workflow_version_id
                    else None
                )
                if run.status == "succeeded" and presentation_policy(version)["enabled"]:
                    presentation = await session.scalar(
                        select(RunPresentation)
                        .where(RunPresentation.run_id == run.id)
                        .order_by(RunPresentation.revision.desc())
                        .limit(1)
                    )
                    if presentation is None or presentation.status not in {
                        "completed",
                        "failed",
                    }:
                        waiting = True
            if waiting:
                continue
        status = (
            "succeeded"
            if any(item.status == "succeeded" for item in all_runs)
            else "cancelled"
            if all(item.status == "cancelled" for item in all_runs)
            else "failed"
        )
        finished = max(
            (item.finished_at for item in all_runs if item.finished_at is not None),
            default=utcnow(),
        )
        presentation_finished = [
            presentation.completed_at
            for run in workflow_runs
            if (
                presentation := await session.scalar(
                    select(RunPresentation)
                    .where(RunPresentation.run_id == run.id)
                    .order_by(RunPresentation.revision.desc())
                    .limit(1)
                )
            )
            is not None
            and presentation.completed_at is not None
        ]
        if presentation_finished:
            finished = max(finished, *presentation_finished)
        await finalize_turn(session, turn.id, status, completed_at=finished)
        completed += 1
    return completed
