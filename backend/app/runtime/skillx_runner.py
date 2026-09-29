from __future__ import annotations

import asyncio
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.workflow import ExecutionError, NodeResult
from app.runtime.skillx import execute_direct, load_interface


class CapabilityClient:
    async def call(self, capability, arguments):
        call_id = str(uuid4())
        sys.__stdout__.write(
            json.dumps(
                {
                    "type": "capability.call",
                    "id": call_id,
                    "capability": capability,
                    "arguments": arguments,
                }
            )
            + "\n"
        )
        sys.__stdout__.flush()
        response = json.loads(await asyncio.to_thread(sys.stdin.buffer.readline))
        if response.get("id") != call_id or response.get("error"):
            raise PermissionError(response.get("error", "依赖响应不匹配"))
        return response["output"]


async def invoke(request: dict[str, Any]) -> NodeResult:
    try:
        loaded = load_interface(
            Path(request["packageRoot"]),
            request["interface"],
        )
        return await execute_direct(
            loaded,
            request["invocationId"],
            request["input"],
            secrets=request.get("secrets", {}),
            capability_client=CapabilityClient(),
        )
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="EXECUTION_FAILED", message=str(exc)),
        )


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.readline())
        with redirect_stdout(sys.stderr):
            result = asyncio.run(invoke(request))
        sys.stdout.write(result.model_dump_json() + "\n")
    except Exception as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
