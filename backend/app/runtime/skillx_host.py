from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.application.secrets import redact_secret_text
from app.domain.workflow import ExecutionError, NodeResult


async def execute_in_subprocess(
    package_root: Path,
    interface_name: str,
    invocation_id: str,
    inputs: dict[str, Any],
    secrets: dict[str, str] | None = None,
) -> NodeResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.runtime.skillx_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request = json.dumps(
        {
            "packageRoot": str(package_root),
            "interface": interface_name,
            "invocationId": invocation_id,
            "input": inputs,
            "secrets": secrets or {},
        },
        ensure_ascii=False,
    ).encode()
    try:
        stdout, stderr = await process.communicate(request)
    except asyncio.CancelledError:
        process.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            process.kill()
        raise
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip() or "SkillX 子进程执行失败"
        message = redact_secret_text(message, list((secrets or {}).values()))
        return NodeResult(
            status="failed",
            error=ExecutionError(code="EXECUTION_FAILED", message=message[-1000:]),
        )
    try:
        payload = json.loads(stdout)
        return NodeResult.model_validate(payload)
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="CONTRACT_VIOLATION",
                message=f"SkillX 子进程返回无效协议: {exc}",
            ),
        )
