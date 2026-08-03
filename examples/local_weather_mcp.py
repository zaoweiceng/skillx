#!/usr/bin/env python3
"""Deterministic local MCP server used by the zero-LLM demo."""

from __future__ import annotations

import json
import sys
from typing import Any

TOOL = {
    "name": "weather.get_current",
    "description": "返回本地演示用的确定性天气数据",
    "inputSchema": {
        "type": "object",
        "properties": {"city": {"type": "string", "minLength": 1}},
        "required": ["city"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "condition": {"type": "string"},
            "temperature": {"type": "number"},
        },
        "required": ["city", "condition", "temperature"],
        "additionalProperties": False,
    },
}


def response(request_id: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            response(
                request["id"],
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "local-weather", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            response(request["id"], {"tools": [TOOL]})
        elif method == "tools/call":
            arguments = request.get("params", {}).get("arguments", {})
            city = arguments["city"]
            conditions = ["晴", "多云", "小雨"]
            output = {
                "city": city,
                "condition": conditions[sum(map(ord, city)) % len(conditions)],
                "temperature": 18 + sum(map(ord, city)) % 16,
            }
            response(
                request["id"],
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(output, ensure_ascii=False),
                        }
                    ],
                    "structuredContent": output,
                    "isError": False,
                },
            )


if __name__ == "__main__":
    main()
