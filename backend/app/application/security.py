from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api[_-]?key|credential)(?:$|_)",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


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
