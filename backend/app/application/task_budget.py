"""Persist active intervals and count their union, excluding human wait time."""

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.infrastructure.models.tasks import Task, TaskEvent


def union_milliseconds(intervals):
    end, duration = float("-inf"), 0.0
    for start, stop in sorted(intervals):
        duration += max(0, stop - max(start, end))
        end = max(end, stop)
    return int(duration * 1000)


def seconds(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


async def record_activity(session, task_id, key, intervals):
    existing = await session.scalar(
        select(TaskEvent.id).where(
            TaskEvent.task_id == task_id,
            TaskEvent.type == "task.activity",
            TaskEvent.payload_json["key"].as_string() == key,
        )
    )
    if not existing:
        session.add(
            TaskEvent(
                task_id=task_id,
                type="task.activity",
                payload_json={
                    "key": key,
                    "intervals": [[seconds(a), seconds(b)] for a, b in intervals],
                },
            )
        )
        await session.flush()
    rows = await session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.type == "task.activity")
    )
    total = union_milliseconds(pair for row in rows for pair in row.payload_json["intervals"])
    await session.execute(update(Task).where(Task.id == task_id).values(active_ms=total))
    return total


def now():
    return datetime.now(UTC)
