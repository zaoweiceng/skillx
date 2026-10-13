"""Monitoring uses durable task/invocation evidence, never browser counters."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.monitoring_series import TimeSeries
from app.application.product_history import elapsed_ms, usage_summary
from app.application.session_records import root, scrub
from app.infrastructure.database import (
    ConversationTurn,
    ModelUsage,
    NodeRun,
    Run,
    get_session,
    rfc3339,
)
from app.infrastructure.models.tasks import Invocation, Task

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
Metric = Literal["tokens", "duration", "success", "skillx", "workflow", "failure", "reasons"]


def window(start: datetime | None, end: datetime | None):
    end = end or datetime.now(UTC)
    start = start or end - timedelta(days=1)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "时间必须包含时区")
    if start >= end:
        raise HTTPException(422, "截止时间必须晚于起始时间")
    return start.astimezone(UTC), end.astimezone(UTC)


@router.get("/metrics/{metric}")
async def metric_value(
    metric: Metric,
    start: datetime | None = None,
    end: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    timezone_offset: int = 0,
):
    start, end = window(start, end)
    if not -840 <= timezone_offset <= 840:
        raise HTTPException(422, "时区偏移超出范围")
    series = TimeSeries(start, end, timezone_offset)
    bounds = (Task.created_at >= start, Task.created_at < end)
    result = {"start": rfc3339(start), "end": rfc3339(end)}
    if metric in {"skillx", "workflow"}:
        times = list(
            await session.scalars(
                select(Invocation.created_at).where(
                    Invocation.kind == metric,
                    Invocation.created_at >= start,
                    Invocation.created_at < end,
                )
            )
        )
        if metric == "workflow":
            times.extend(
                await session.scalars(
                    select(Run.created_at).where(
                        Run.workflow_version_id.is_not(None),
                        Run.created_at >= start,
                        Run.created_at < end,
                        ~select(Invocation.id).where(Invocation.run_id == Run.id).exists(),
                    )
                )
            )
        else:
            # Attribute a retried node only to its first actual execution, even across buckets.
            nodes = (
                select(func.min(NodeRun.started_at).label("started_at"))
                .where(NodeRun.node_type == "skillx", NodeRun.started_at.is_not(None))
                .group_by(NodeRun.run_id, NodeRun.node_id)
                .subquery()
            )
            times.extend(
                await session.scalars(
                    select(nodes.c.started_at).where(
                        nodes.c.started_at >= start, nodes.c.started_at < end
                    )
                )
            )
        for timestamp in times:
            series.add(timestamp, 1)
        return {
            **result,
            **series.view(),
            "value": len(times),
            "basis": "按调用创建时间，包含失败调用；节点按首次执行时间，重试不重复计数",
        }
    task_states = (await session.execute(select(Task.created_at, Task.status).where(*bounds))).all()
    counts: dict[str, int] = {}
    for timestamp, status in task_states:
        counts[status] = counts.get(status, 0) + 1
        if status in {"succeeded", "failed", "blocked"}:
            series.add(
                timestamp,
                int(status == "succeeded")
                if metric == "success"
                else int(status in {"failed", "blocked"}),
            )
    succeeded = counts.get("succeeded", 0)
    failed = counts.get("failed", 0) + counts.get("blocked", 0)
    completed = succeeded + failed
    result.update(
        {
            "tasks": sum(counts.values()),
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": counts.get("cancelled", 0),
            "completed": completed,
            "basis": "按任务创建时间；成功/失败率以成功、失败及阻塞的任务为分母，取消单列",
        }
    )
    if metric in {"success", "failure"}:
        return {
            **result,
            **series.view(average=True),
            "value": (succeeded if metric == "success" else failed) / completed
            if completed
            else None,
        }
    if metric == "duration":
        rows = (
            await session.execute(
                select(
                    Task.id,
                    Task.goal,
                    Task.status,
                    Task.conversation_id,
                    Task.created_at,
                    ConversationTurn.accepted_at,
                    ConversationTurn.completed_at,
                )
                .join(Task, Task.turn_id == ConversationTurn.id)
                .where(
                    *bounds,
                    Task.status.in_(["succeeded", "failed", "blocked"]),
                    ConversationTurn.completed_at.is_not(None),
                )
            )
        ).all()
        series = TimeSeries(start, end, timezone_offset)
        durations = [elapsed_ms(row.accepted_at, row.completed_at) for row in rows]
        for row in rows:
            series.add(row.created_at, elapsed_ms(row.accepted_at, row.completed_at))
        ranking = sorted(
            [
                {
                    "id": row.id,
                    "goal": scrub(row.goal),
                    "status": row.status,
                    "conversationId": row.conversation_id,
                    "createdAt": rfc3339(row.created_at),
                    "durationMs": elapsed_ms(row.accepted_at, row.completed_at),
                }
                for row in rows
            ],
            key=lambda item: (-item["durationMs"], item["createdAt"], item["id"]),
        )
        return {
            **result,
            **series.view(average=True),
            "value": sum(durations) / len(durations) if durations else None,
            "samples": len(durations),
            "ranking": ranking,
            "basis": "按耗时从长到短排列；从任务接收到完成，包含等待时间，取消除外",
        }
    if metric == "reasons":
        errors = (
            await session.execute(
                select(Task.error_json, Task.output_json).where(
                    *bounds, Task.status.in_(["failed", "blocked"])
                )
            )
        ).all()
        reasons: dict[str, int] = {}
        examples: dict[str, str] = {}
        for error, output in errors:
            error = error or {}
            reason = str(
                error.get("code")
                or error.get("message")
                or error.get("summary")
                or (output or {}).get("text")
                or "未记录失败原因"
            )
            reasons[reason] = reasons.get(reason, 0) + 1
            examples[reason] = str(
                error.get("message") or error.get("summary") or (output or {}).get("text") or ""
            )
        return {
            **result,
            "items": [
                {"reason": scrub(reason), "count": count, "example": scrub(examples[reason])}
                for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0]))
            ],
        }
    tasks = (await session.scalars(select(Task).where(*bounds))).all()
    total, incomplete = 0, 0
    series = TimeSeries(start, end, timezone_offset)
    for task in tasks:
        summary = (
            await usage_summary(session, task.turn_id)
            if task.turn_id
            else {"totalTokens": task.total_tokens, "state": "partial"}
        )
        total += summary.get("totalTokens") or 0
        series.add(task.created_at, summary.get("totalTokens") or 0)
        incomplete += summary["state"] in {"partial", "unavailable", "pending"}
    # Standalone workflow/model calls are not part of Task counters.
    orphan_rows = (
        await session.execute(
            select(ModelUsage.created_at, ModelUsage.total_tokens).where(
                ModelUsage.created_at >= start,
                ModelUsage.created_at < end,
                ~select(Task.id).where(Task.turn_id == ModelUsage.turn_id).exists(),
                ~select(Invocation.id)
                .where(Invocation.run_id == ModelUsage.run_id, Invocation.task_id.is_not(None))
                .exists(),
            )
        )
    ).all()
    orphan_total = 0
    for timestamp, tokens in orphan_rows:
        orphan_total += tokens or 0
        series.add(timestamp, tokens or 0)
    return {
        **result,
        **series.view(),
        "value": total + orphan_total,
        "incompleteTasks": incomplete,
        "basis": "按任务创建时间汇总任务及学习用量；独立模型调用按记录时间；未报告用量不估算",
    }


def _files(start: datetime, end: datetime):
    day = start.date()
    # Only visit existing date directories, even for large custom windows.
    for path in sorted(root().glob("*/*/*/rollout-*.jsonl"), reverse=True):
        try:
            date = datetime.strptime("/".join(path.parts[-4:-1]), "%Y/%m/%d").date()
        except ValueError:
            continue
        if day <= date <= end.date():
            yield path


@router.get("/sessions")
def sessions(
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=100),
):
    start, end = window(start, end)
    items = []
    for path in _files(start, end):
        try:
            with path.open(encoding="utf-8") as stream:
                meta = json.loads(stream.readline())
            timestamp = datetime.fromisoformat(meta["timestamp"])
            if start <= timestamp < end:
                items.append(
                    {
                        **meta["payload"],
                        "createdAt": meta["timestamp"],
                        "bytes": path.stat().st_size,
                    }
                )
        except (OSError, ValueError, KeyError):
            continue
    items.sort(key=lambda item: (item["createdAt"], item["id"]), reverse=True)
    return {"items": items[offset : offset + limit], "total": len(items)}


@router.get("/sessions/{session_id}")
def session_records(
    session_id: UUID, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)
):
    paths = list(root().glob(f"*/*/*/rollout-{session_id}.jsonl"))
    if not paths:
        raise HTTPException(404, "Session 不存在；历史执行不会补造原始记录")
    records = []
    next_offset = None
    with paths[0].open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            if index < offset:
                continue
            if len(records) == limit:
                next_offset = index
                break
            try:
                records.append(json.loads(line))
            except ValueError:
                # A live writer may still be appending this line; retry it next time.
                next_offset = index
                break
    return {"items": records, "nextOffset": next_offset}
