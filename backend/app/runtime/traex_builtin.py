from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Request, Response
from jsonschema import Draft202012Validator

from app.config import get_settings
from app.runtime.agent_contracts import AdapterError


class TraeXBridgeError(AdapterError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _trusted_working_directory(working_directory: Path) -> Path:
    """Resolve the per-run working directory and confirm it stays inside the
    platform artifact root.

    平台为每一轮创建的隔离目录才可作为 TraeX ``-C`` 与 subprocess cwd。任何越界路径、
    symlink 逃逸或任意外部 cwd 都 fail closed（``TRAE_WORKDIR_UNTRUSTED``），不改由仓库或
    用户目录执行，也不放宽沙箱。``resolve()`` 会展开全部 symlink 组件，逃逸出根目录的软链因此
    在此被拒。
    """

    root = get_settings().artifact_root.resolve()
    resolved = working_directory.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise TraeXBridgeError(
            "TRAE_WORKDIR_UNTRUSTED",
            "TraeX 工作目录不在平台本轮隔离根目录内，已拒绝执行",
        )
    return resolved


def _tool_name(capability_ref: str) -> str:
    """Build a stable, MCP/TraeX-safe tool name that is one-to-one with the ref.

    非字母数字字符被规范化后可能碰撞（``a.b`` 与 ``a_b``），因此再附加 capability ref 的
    digest 后缀，使名称与原始 ref 一一对应，同时仍满足 MCP/TraeX 的字符与长度限制。
    """

    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", capability_ref)[:48].strip("_") or "cap"
    suffix = hashlib.sha256(capability_ref.encode("utf-8")).hexdigest()[:12]
    return f"{slug}__{suffix}"


def _tool_descriptor_map(
    tool_descriptors: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build the ``toolName → descriptor`` map with an explicit reverse mapping.

    构建时检测重复：即便加了 digest 后缀，任何名称碰撞都 fail closed，不允许字典推导静默
    覆盖能力。Tool call/ToolResult/provenance 仍使用 descriptor 内原始 ``capabilityRef``。
    """

    descriptors: dict[str, dict[str, Any]] = {}
    reverse: dict[str, str] = {}
    for item in tool_descriptors:
        capability_ref = str(item["capabilityRef"])
        name = _tool_name(capability_ref)
        existing = reverse.get(name)
        if existing is not None and existing != capability_ref:
            raise TraeXBridgeError(
                "AGENT_PROTOCOL_INCOMPATIBLE",
                f"MCP tool 名称碰撞: {existing!r} 与 {capability_ref!r} 归一化为同名 {name!r}",
            )
        descriptors[name] = item
        reverse[name] = capability_ref
    return descriptors


def _safe_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "CODEX_HOME",
        "TRAE_HOME",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _error_code(stderr: str, events: list[dict[str, Any]]) -> str:
    text = (
        stderr
        + "\n"
        + "\n".join(json.dumps(item, ensure_ascii=False) for item in events)
    ).lower()
    if any(marker in text for marker in (
        "401", "unauthorized", "access token expired", "access token has expired"
    )):
        return "TRAE_AUTH_REQUIRED"
    if any(
        marker in text
        for marker in ("trusted directory", "skip-git-repo-check", "not inside a")
    ):
        return "TRAE_WORKDIR_UNTRUSTED"
    if any(marker in text for marker in ("model unavailable", "model not found")):
        return "TRAE_MODEL_UNAVAILABLE"
    if any(marker in text for marker in ("429", "rate limit", "too many requests")):
        return "RATE_LIMITED"
    if "timed out" in text or "timeout" in text:
        return "TIMEOUT"
    return "AGENT_PROTOCOL_INCOMPATIBLE"


def _usage(event: dict[str, Any]) -> dict[str, int]:
    raw = event.get("usage")
    if not isinstance(raw, dict):
        return {}
    input_tokens = int(raw.get("input_tokens") or raw.get("inputTokens") or 0)
    output_tokens = int(raw.get("output_tokens") or raw.get("outputTokens") or 0)
    total_tokens = int(
        raw.get("total_tokens")
        or raw.get("totalTokens")
        or input_tokens + output_tokens
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _safe_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Keep protocol evidence without retaining prompts, messages or tool payloads."""

    summary: dict[str, Any] = {"type": str(event.get("type") or "<missing>")[:80]}
    for key in ("version", "thread_id", "turn_id", "request_id"):
        value = event.get(key)
        if isinstance(value, (str, int)):
            summary[key] = str(value)[:120]
    item = event.get("item")
    if isinstance(item, dict):
        summary["itemType"] = str(item.get("type") or "<missing>")[:80]
        for key in ("id", "request_id"):
            value = item.get(key)
            if isinstance(value, (str, int)):
                summary[f"item_{key}"] = str(value)[:120]
    if isinstance(event.get("usage"), dict):
        summary["usage"] = _usage(event)
    return summary


def _native_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            if error.get(key):
                return str(error[key])[:2000]
    if isinstance(error, str):
        return error[:2000]
    for key in ("message", "detail"):
        if event.get(key):
            return str(event[key])[:2000]
    return "TraeX 原生事件报告运行失败"


def _runtime_identity(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    provider: str | None = None
    model: str | None = None
    for event in events:
        candidates = [event, event.get("thread"), event.get("turn"), event.get("item")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_provider = candidate.get("modelProviderId") or candidate.get(
                "model_provider_id"
            )
            raw_model = candidate.get("model")
            if isinstance(raw_provider, str) and raw_provider:
                provider = raw_provider
            if isinstance(raw_model, str) and raw_model:
                model = raw_model
    return provider, model


class TraeXBuiltinAdapter:
    """Bridge the native ``traex exec`` JSONL/MCP protocol to AgentHost events."""

    supports_tool_descriptors = True

    def __init__(self, command: list[str], model: str | None = None) -> None:
        if not command or Path(command[0]).name != "traex":
            raise TraeXBridgeError("TRAE_NOT_INSTALLED", "未发现受支持的 traex")
        self.command = command
        self.model = model
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tool_results: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._tool_evidence: dict[str, list[dict[str, Any]]] = {}

    async def events(self, run_id: str, request: Any) -> AsyncIterator[dict[str, Any]]:
        # 只信任平台本轮 artifact 根目录内的隔离目录；越界/symlink 逃逸 fail closed。
        working_directory = _trusted_working_directory(request.working_directory)
        working_directory.mkdir(parents=True, exist_ok=True)
        run_key = re.sub(r"[^a-zA-Z0-9_-]", "_", run_id)[-80:]
        schema_path = working_directory / f".traex-schema-{run_key}.json"
        output_path = working_directory / f".traex-output-{run_key}.json"
        schema_path.write_text(
            json.dumps(request.output_schema, ensure_ascii=False),
            encoding="utf-8",
        )
        descriptors = _tool_descriptor_map(request.tool_descriptors)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        app = self._mcp_app(run_id, descriptors, queue)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=None,
                access_log=False,
                lifespan="off",
                timeout_graceful_shutdown=1,
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        for _ in range(100):
            if server.started:
                break
            if server_task.done():
                raise TraeXBridgeError(
                    "AGENT_PROTOCOL_INCOMPATIBLE",
                    "本轮 TraeX MCP bridge 启动失败",
                )
            await asyncio.sleep(0.01)

        allowed_tools = [
            value
            for name in descriptors
            for value in ("--allowed-tool", f"mcp__skillx__{name}")
        ]
        if not allowed_tools:
            allowed_tools = ["--allowed-tool", "mcp__skillx____no_tools__"]
        command = [
            *self.command,
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ignore-rules",
            # 工作目录已确认位于平台本轮隔离根目录内（非 Git 仓库）：显式跳过 Git 仓库检查，
            # 但保留 read-only sandbox、忽略用户配置/规则、空 hooks/plugins/skills 与 allowlist；
            # 跳过 Git 检查不等于放宽文件或工具权限。
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-C",
            str(working_directory),
            "-c",
            f'mcp_servers.skillx.url="http://127.0.0.1:{port}/mcp"',
            "-c",
            "mcp_servers.skillx.required=true",
            "-c",
            "hooks={}",
            "-c",
            "plugins={}",
            "-c",
            "skills={}",
            *(["--model", self.model] if self.model else []),
            *allowed_tools,
            request.prompt,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=working_directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_safe_environment(),
        )
        self._processes[run_id] = process
        self._tool_evidence[run_id] = []
        raw_events: list[dict[str, Any]] = []
        safe_event_summaries: list[dict[str, Any]] = []
        stderr_task = asyncio.create_task(process.stderr.read())
        dispatched = False
        usage: dict[str, int] = {}
        last_message = ""
        turn_completed = False
        native_failure: dict[str, Any] | None = None
        recognized_events = 0
        try:
            if process.stdout is None:
                raise TraeXBridgeError(
                    "AGENT_PROTOCOL_INCOMPATIBLE", "TraeX stdout 不可用"
                )
            stdout_task = asyncio.create_task(process.stdout.readline())
            tool_task = asyncio.create_task(queue.get())
            while True:
                done, _ = await asyncio.wait(
                    {stdout_task, tool_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if tool_task in done:
                    yield tool_task.result()
                    tool_task = asyncio.create_task(queue.get())
                if stdout_task not in done:
                    continue
                line = stdout_task.result()
                if not line:
                    break
                stdout_task = asyncio.create_task(process.stdout.readline())
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise TraeXBridgeError(
                        "AGENT_PROTOCOL_INCOMPATIBLE",
                        "TraeX JSONL 包含无法解析的事件",
                    ) from exc
                if not isinstance(event, dict):
                    raise TraeXBridgeError(
                        "AGENT_PROTOCOL_INCOMPATIBLE",
                        "TraeX JSONL 事件不是对象",
                    )
                raw_events.append(event)
                safe_event_summaries.append(_safe_event_summary(event))
                event_type = event.get("type")
                item = event.get("item") or {}
                if event_type in {
                    "thread.started",
                    "turn.started",
                    "item.started",
                    "item.completed",
                    "turn.completed",
                    "turn.failed",
                    "error",
                }:
                    recognized_events += 1
                if (
                    event_type in {"item.started", "item.completed"}
                    and item.get("type")
                    in {"agent_message", "reasoning", "mcp_tool_call"}
                    and not dispatched
                ):
                    dispatched = True
                    yield {
                        "type": "model.request.dispatched",
                        "payload": {"attempt": 1},
                    }
                if event_type == "turn.completed":
                    turn_completed = True
                    usage = _usage(event)
                elif event_type in {"turn.failed", "error"}:
                    # Native failed terminals are authoritative even when the
                    # CLI exits 0 or happened to leave an output file behind.
                    native_failure = event
                    usage = _usage(event) or usage
                elif event_type == "item.completed":
                    if item.get("type") == "agent_message":
                        last_message = str(item.get("text") or "")
            tool_task.cancel()
            with suppress(asyncio.CancelledError):
                await tool_task
            code = await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(
                usage.get("total_tokens") or input_tokens + output_tokens
            )
            if input_tokens or output_tokens:
                yield {
                    "type": "agent.step.completed",
                    "payload": {
                        "step": 1,
                        "inputTokens": input_tokens,
                        "outputTokens": output_tokens,
                    },
                }
            if native_failure is not None:
                native_message = _native_error_message(native_failure)
                seen_types = ",".join(
                    str(item.get("type")) for item in safe_event_summaries[-12:]
                )
                message = (
                    f"{native_message} [nativeTerminal={native_failure.get('type')};"
                    f" events={seen_types}]"
                )
                error_code = _error_code(
                    f"{stderr}\n{message}", safe_event_summaries
                )
                raise TraeXBridgeError(error_code, message)
            if code != 0:
                error_code = _error_code(stderr, safe_event_summaries)
                # 认证失败、模型不可用、限流或工作目录不受信都可能发生在任何模型请求之前
                # （甚至在网关预检阶段）。没有 TraeX 明确 dispatch 事件时不得伪造
                # model.request.dispatched：dispatched 保持 false，attempted 由上层单独记录。
                raise TraeXBridgeError(
                    error_code,
                    stderr.strip() or f"TraeX 进程退出码: {code}",
                )
            if not turn_completed:
                evidence = ", ".join(
                    str(item.get("type")) for item in safe_event_summaries[-8:]
                )
                raise TraeXBridgeError(
                    "AGENT_PROTOCOL_INCOMPATIBLE",
                    "TraeX 未报告受支持的 turn.completed 终态"
                    + (f"；已见事件: {evidence}" if recognized_events else ""),
                )
            output_text = (
                output_path.read_text(encoding="utf-8")
                if output_path.is_file()
                else last_message
            )
            try:
                output = json.loads(output_text)
            except json.JSONDecodeError as exc:
                raise TraeXBridgeError(
                    "MODEL_MALFORMED_OUTPUT",
                    "TraeX 未返回严格 JSON 终态",
                ) from exc
            if not isinstance(output, dict):
                raise TraeXBridgeError(
                    "MODEL_MALFORMED_OUTPUT",
                    "TraeX 严格 JSON 终态不是对象",
                )
            schema_errors = list(
                Draft202012Validator(request.output_schema).iter_errors(output)
            )
            if schema_errors:
                raise TraeXBridgeError(
                    "MODEL_MALFORMED_OUTPUT",
                    f"TraeX JSON 终态不符合 output schema: {schema_errors[0].message}",
                )
            evidence = self._tool_evidence.get(run_id, [])
            provider, model = _runtime_identity(raw_events)
            provenance = {
                field: {
                    "mode": "generative",
                    "sourceCallIds": [item["callId"] for item in evidence],
                    "evidenceDigests": [
                        item["evidenceDigest"] for item in evidence
                    ],
                    "promptVersion": "traex-builtin/v1",
                }
                for field in output
            } if evidence else {}
            yield {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": total_tokens,
                    "usageComplete": bool(input_tokens or output_tokens),
                    "output": output,
                    "outputProvenance": provenance,
                    "provider": provider or "traex",
                    "model": model,
                    "nativeProtocol": {
                        "terminal": "turn.completed",
                        "events": safe_event_summaries[-40:],
                    },
                },
            }
        finally:
            if process.returncode is None:
                process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=5)
            server.should_exit = True
            for key, future in list(self._tool_results.items()):
                if key[0] == run_id:
                    if not future.done():
                        future.cancel()
                    self._tool_results.pop(key, None)
            with suppress(asyncio.CancelledError):
                await server_task
            sock.close()
            schema_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            self._processes.pop(run_id, None)
            self._tool_evidence.pop(run_id, None)

    def _mcp_app(
        self,
        run_id: str,
        descriptors: dict[str, dict[str, Any]],
        queue: asyncio.Queue[dict[str, Any]],
    ) -> FastAPI:
        app = FastAPI()

        @app.post("/mcp")
        async def mcp(request: Request) -> Any:
            payload = await request.json()
            request_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "skillx-run-bridge",
                            "version": "1",
                        },
                    },
                }
            if method == "notifications/initialized":
                return Response(status_code=202)
            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": name,
                                "description": (
                                    "SkillX Host capability "
                                    f"{descriptor['capabilityRef']}"
                                ),
                                "inputSchema": descriptor["inputSchema"],
                                "outputSchema": descriptor["outputSchema"],
                                "annotations": {"readOnlyHint": True},
                            }
                            for name, descriptor in descriptors.items()
                        ]
                    },
                }
            if method == "tools/call":
                params = payload.get("params") or {}
                descriptor = descriptors.get(str(params.get("name")))
                if descriptor is None:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "Tool 不在本轮能力集合"},
                    }
                call_id = str(uuid4())
                future = asyncio.get_running_loop().create_future()
                self._tool_results[(run_id, call_id)] = future
                await queue.put(
                    {
                        "type": "tool.requested",
                        "payload": {
                            "callId": call_id,
                            "capabilityRef": descriptor["capabilityRef"],
                            "arguments": dict(params.get("arguments") or {}),
                        },
                    }
                )
                result = await future
                result_payload = result["payload"]
                if result_payload["status"] == "succeeded":
                    output = result_payload.get("output") or {}
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        output, ensure_ascii=False
                                    ),
                                }
                            ],
                            "structuredContent": output,
                            "isError": False,
                        },
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    result_payload.get("error") or {},
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            }

        return app

    async def send_tool_result(
        self, run_id: str, result: dict[str, Any]
    ) -> None:
        call_id = str(result.get("payload", {}).get("callId", ""))
        future = self._tool_results.pop((run_id, call_id), None)
        if future is None or future.done():
            raise TraeXBridgeError(
                "AGENT_PROTOCOL_INCOMPATIBLE",
                "TraeX MCP ToolResult 无匹配 callId",
            )
        payload = result["payload"]
        if payload.get("status") == "succeeded":
            self._tool_evidence.setdefault(run_id, []).append(
                {
                    "callId": call_id,
                    "evidenceDigest": payload["evidenceDigest"],
                }
            )
        future.set_result(result)

    async def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if process is None or process.returncode is not None:
            return
        process.terminate()
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
