from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.domain.workflow import ExecutionError, NodeResult
from app.runtime.skillx import execute_direct, load_interface


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
        )
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="EXECUTION_FAILED", message=str(exc)),
        )


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        result = asyncio.run(invoke(request))
        sys.stdout.write(result.model_dump_json())
    except Exception as exc:
        sys.stderr.write(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
