from __future__ import annotations

import json
import re
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api[_-]?key|credential)(?:$|_)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"
MAX_TECHNICAL_DETAIL_CHARS = 4000
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w.-])/(?:Users|home|tmp|private|var|opt|Applications)/[^\s\"'<>]+"
)
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|api[_-]?key|secret|password)=)[^&#\s]+"
)


def redact(value: Any, schema: dict[str, Any] | None = None) -> Any:
    if schema and (
        schema.get("writeOnly") is True
        or schema.get("x-skillx-sensitive") is True
    ):
        return REDACTED
    if isinstance(value, dict):
        properties = schema.get("properties", {}) if schema else {}
        additional = schema.get("additionalProperties") if schema else None
        return {
            key: (
                REDACTED
                if SENSITIVE_KEY.search(str(key))
                else redact(
                    item,
                    properties.get(key)
                    or (additional if isinstance(additional, dict) else None),
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        item_schema = schema.get("items") if schema else None
        return [
            redact(item, item_schema if isinstance(item_schema, dict) else None)
            for item in value
        ]
    return value


def event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = redact(payload)
    for key in ("input", "output"):
        value = sanitized.get(key)
        if isinstance(value, dict):
            sanitized[key] = {
                "summary": "structured-object",
                "keys": sorted(value.keys()),
            }
    return sanitized


def sanitize_technical_detail(value: Any) -> Any:
    """Redact and bound diagnostic evidence before it is persisted or exposed."""

    sanitized = redact(value)
    if isinstance(sanitized, dict):
        return {
            str(key): sanitize_technical_detail(item)
            for key, item in sanitized.items()
        }
    if isinstance(sanitized, list):
        return [sanitize_technical_detail(item) for item in sanitized[:100]]
    if not isinstance(sanitized, str):
        return sanitized
    text = _AUTHORIZATION.sub(r"\1: [REDACTED]", sanitized)
    text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
    text = _ABSOLUTE_PATH.sub("[PATH]", text)
    if len(text) > MAX_TECHNICAL_DETAIL_CHARS:
        text = f"{text[:MAX_TECHNICAL_DETAIL_CHARS]}...[TRUNCATED]"
    return text


def bounded_technical_json(value: Any) -> Any:
    """Keep the complete projection small even for deeply nested provider logs."""

    if (
        isinstance(value, dict)
        and value.get("truncated") is True
        and isinstance(value.get("summary"), str)
        and set(value).issubset({"summary", "truncated"})
    ):
        return {
            "summary": sanitize_technical_detail(value["summary"]),
            "truncated": True,
        }
    sanitized = sanitize_technical_detail(value)
    encoded = json.dumps(sanitized, ensure_ascii=False, default=str)
    if len(encoded) <= MAX_TECHNICAL_DETAIL_CHARS:
        return sanitized
    return {
        "summary": encoded[:MAX_TECHNICAL_DETAIL_CHARS],
        "truncated": True,
    }
