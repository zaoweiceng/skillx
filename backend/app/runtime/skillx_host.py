from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.application.secrets import contains_secret, redact_secret_text
from app.config import get_settings
from app.domain.workflow import ExecutionError, NodeResult


async def execute_in_subprocess(
    package_root: Path,
    interface_name: str,
    invocation_id: str,
    inputs: dict[str, Any],
    secrets: dict[str, str] | None = None,
    dependency_handler=None,
) -> NodeResult:
    workdir = get_settings().artifact_root.resolve() / "skillx-execution" / invocation_id
    workdir.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.defpath,
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.runtime.skillx_runner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024 * 1024,
        cwd=workdir,
        env=environment,
        start_new_session=True,
    )
    request = {
        "packageRoot": str(package_root.resolve()),
        "interface": interface_name,
        "invocationId": invocation_id,
        "input": inputs,
        "secrets": secrets or {},
    }
    assert process.stdin and process.stdout and process.stderr

    # Drain stderr concurrently, retaining only a bounded tail.
    async def read_errors():
        tail = b""
        while chunk := await process.stderr.read(4096):
            tail = (tail + chunk)[-16000:]
        return tail

    errors = asyncio.create_task(read_errors())
    try:
        process.stdin.write(json.dumps(request).encode() + b"\n")
        await process.stdin.drain()
        payload = None
        count = 0
        while line := await process.stdout.readline():
            value = json.loads(line)
            if value.get("type") == "capability.call":
                count += 1
                response = {"id": value.get("id")}
                try:
                    if not dependency_handler or count > 64:
                        raise PermissionError("依赖未绑定或超过调用预算")
                    response["output"] = await dependency_handler(
                        value["capability"], value["arguments"]
                    )
                except Exception as exc:
                    response["error"] = str(exc)
                process.stdin.write(json.dumps(response).encode() + b"\n")
                await process.stdin.drain()
            else:
                payload = value
        await process.wait()
        stderr = await errors
        if process.returncode:
            message = redact_secret_text(
                stderr.decode(errors="replace"), list((secrets or {}).values())
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(code="EXECUTION_FAILED", message=message[-1000:]),
            )
        result = NodeResult.model_validate(payload)
        if contains_secret(result.model_dump(), list((secrets or {}).values())):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION", message="SkillX 输出包含 Secret，已阻止持久化"
                ),
            )
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="CONTRACT_VIOLATION",
                message=redact_secret_text(str(exc), list((secrets or {}).values())),
            ),
        )
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await errors
