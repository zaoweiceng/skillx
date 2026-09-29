"""Content-addressed large JSON fields with transparent, verified hydration."""

import hashlib
import json
import os
from uuid import uuid4

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

MARKER = "$skillxArtifact/v1"
LITERAL = "$skillxLiteral/v1"
THRESHOLD = 64 * 1024


def encode(value):
    if isinstance(value, dict) and (MARKER in value or LITERAL in value):
        return {LITERAL: value}
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    if len(payload) < THRESHOLD:
        return value
    digest = hashlib.sha256(payload).hexdigest()
    root = get_settings().artifact_root.resolve() / "payloads"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (digest + ".json")
    temporary = root / (digest + "." + str(uuid4()) + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)
    return {MARKER: {"sha256": digest, "size": len(payload), "mediaType": "application/json"}}


def decode(value):
    if isinstance(value, dict) and set(value) == {LITERAL}:
        return value[LITERAL]
    if not isinstance(value, dict) or set(value) != {MARKER}:
        return value
    reference = value[MARKER]
    digest = reference["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Artifact digest 无效")
    path = get_settings().artifact_root.resolve() / "payloads" / (digest + ".json")
    payload = path.read_bytes()
    if len(payload) != reference["size"] or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Artifact 内容校验失败")
    return json.loads(payload)


class ArtifactJSON(TypeDecorator):
    """Retain top-level indexed keys; offload large values, including messages/code."""

    impl = JSON
    cache_ok = True

    def coerce_compared_value(self, op, value):
        return self.impl.coerce_compared_value(op, value)

    def process_bind_param(self, value, dialect):
        if isinstance(value, dict) and (MARKER in value or LITERAL in value):
            return {LITERAL: value}
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        return encode(value)

    def process_result_value(self, value, dialect):
        if isinstance(value, dict) and set(value) not in ({MARKER}, {LITERAL}):
            return {key: decode(item) for key, item in value.items()}
        return decode(value)


async def task_payload_references(session, task_id):
    """Read storage envelopes only from records belonging to this task."""
    from sqlalchemy import Text, select

    from app.infrastructure.database import NodeRun, Run, RunEvent
    from app.infrastructure.models.tasks import (
        HumanInteraction,
        Invocation,
        LearningJob,
        Task,
        TaskEvent,
    )

    run_ids = select(Invocation.run_id).where(Invocation.task_id == task_id)
    sources = [
        (Task, Task.id == task_id),
        (Invocation, Invocation.task_id == task_id),
        (TaskEvent, TaskEvent.task_id == task_id),
        (HumanInteraction, HumanInteraction.task_id == task_id),
        (LearningJob, LearningJob.task_id == task_id),
        (Run, Run.id.in_(run_ids)),
        (NodeRun, NodeRun.run_id.in_(run_ids)),
        (RunEvent, RunEvent.run_id.in_(run_ids)),
    ]
    references = {}
    for model, condition in sources:
        columns = [c for c in model.__table__.columns if isinstance(c.type, ArtifactJSON)]
        if not columns:
            continue
        rows = await session.execute(select(*(c.cast(Text) for c in columns)).where(condition))
        for row in rows:
            for raw in row:
                value = json.loads(raw) if raw is not None else None
                # Only these two envelope levels are written by ArtifactJSON. Never follow
                # similarly named keys inside user-controlled JSON (including literal escapes).
                values = (
                    value.values()
                    if isinstance(value, dict) and MARKER not in value and LITERAL not in value
                    else [value]
                )
                for item in values:
                    if isinstance(item, dict) and set(item) == {MARKER}:
                        reference = item[MARKER]
                        references[reference["sha256"]] = reference
    return references
