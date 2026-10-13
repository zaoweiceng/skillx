"""Append-only, per-execution JSONL transcripts, using TraeX's envelope shape."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

from app.application.security import REDACTED, SENSITIVE_KEY
from app.config import get_settings

logger = logging.getLogger(__name__)
_current: ContextVar[Path | None] = ContextVar("session_record", default=None)
_SECRET_TEXT = re.compile(
    r"(?i)(authorization[\"\s]*[:=][\"\s]*(?:bearer\s+)?|"
    r"(?:api[_-]?key|access_token|password)[\"\s]*[:=][\"\s]*)[^\s\",;}]+"
)


def scrub(value):
    if isinstance(value, dict):
        return {
            k: REDACTED
            if (
                SENSITIVE_KEY.search(str(k))
                or str(k).lower()
                in {"authorization", "proxy-authorization", "cookie", "set-cookie"}
            )
            else scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub(r"\1[REDACTED]", value)
    return value


def root() -> Path:
    return get_settings().artifact_root / "sessions"


def _append(path: Path, kind: str, payload: object):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": kind,
                    "payload": scrub(payload),
                },
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )


async def record(kind: str, payload: object):
    path = _current.get()
    if path is not None:
        try:
            await asyncio.to_thread(_append, path, kind, payload)
        except OSError:
            logger.exception("Session record write failed: %s", path.name)


def recorded_session(fn):
    @wraps(fn)
    async def wrapped(self, request, *args, **kwargs):
        now = datetime.now(UTC)
        session_id = str(uuid4())
        path = root() / now.strftime("%Y/%m/%d") / f"rollout-{session_id}.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=False)
        except OSError:
            logger.exception("Session record creation failed")
            return await fn(self, request, *args, **kwargs)
        token = _current.set(path)
        try:
            await record(
                "session_meta",
                {
                    "id": session_id,
                    "runId": request.run_id,
                    "timestamp": now.isoformat(),
                    "cwd": str(request.working_directory),
                    "originator": "skillx",
                    "schemaVersion": 1,
                },
            )
            await record(
                "turn_context",
                {
                    "prompt": request.prompt,
                    "outputSchema": request.output_schema,
                    "requestedCapabilities": request.requested_capabilities,
                },
            )
            result = await fn(self, request, *args, **kwargs)
            await record(
                "session_end",
                {
                    "status": "succeeded",
                    "output": result.output,
                    "totalTokens": result.total_tokens,
                    "usageComplete": result.usage_complete,
                },
            )
            return result
        except BaseException as exc:
            await record(
                "session_end",
                {
                    "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                },
            )
            raise
        finally:
            _current.reset(token)

    return wrapped
