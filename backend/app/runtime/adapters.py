from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.application.runtime_config import AgentRuntimeConfig, agent_runtime_config
from app.infrastructure.runtime_credentials import resolve_runtime_key
from app.runtime.agent_contracts import AdapterError as AdapterError
from app.runtime.agent_contracts import AgentRunRequest as AgentRunRequest


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    # MCP tools/list 可选返回的 annotations（如 readOnlyHint / destructiveHint /
    # idempotentHint）。仅作为提示，最终 effect/idempotent 由本地 Registry 审核决定。
    annotations: dict[str, Any] = field(default_factory=dict)


class MCPGateway(Protocol):
    async def discover(self) -> list[ToolDescriptor]: ...

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class StreamableHttpMCPGateway:
    """MCP Streamable HTTP 传输。

    实现 initialize 握手、协议版本协商、会话 id（Mcp-Session-Id）跟踪，并解析
    application/json 与 text/event-stream 两类响应。
    """

    PROTOCOL_VERSION = "2025-03-26"

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout_seconds
        self._request_id = 0
        self._session_id: str | None = None
        self._initialized = False

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
            "mcp-protocol-version": self.PROTOCOL_VERSION,
            **self.headers,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    @staticmethod
    def _parse_sse(body: str) -> Any:
        """从 text/event-stream 响应中提取最后一个 JSON-RPC data 事件。"""

        payload: Any = None
        data_lines: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
            elif line == "" and data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = None
                data_lines = []
        if data_lines:
            with suppress(json.JSONDecodeError):
                payload = json.loads("\n".join(data_lines))
        return payload

    def _decode(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._parse_sse(response.text)
        return response.json()

    async def _ensure_initialized(self, client: httpx.AsyncClient) -> None:
        if self._initialized:
            return
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "skillx-workflow", "version": "0.1.0"},
            },
        }
        response = await client.post(self.url, headers=self._base_headers(), json=request)
        response.raise_for_status()
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        payload = self._decode(response)
        if isinstance(payload, dict) and "error" in payload:
            raise AdapterError(payload["error"].get("message", "MCP initialize 失败"))
        # initialized 通知，无响应体。
        await client.post(
            self.url,
            headers=self._base_headers(),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self._initialized = True

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await self._ensure_initialized(client)
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params or {},
            }
            response = await client.post(self.url, headers=self._base_headers(), json=request)
            # 会话过期（服务端返回 404 Not Found）时，重置会话并重新握手一次后重试；
            # 这是 Streamable HTTP 断线重连的最小实现，避免因 session 失效整次失败。
            if response.status_code == 404 and self._session_id is not None:
                self._session_id = None
                self._initialized = False
                await self._ensure_initialized(client)
                response = await client.post(
                    self.url, headers=self._base_headers(), json=request
                )
            response.raise_for_status()
            payload = self._decode(response)
        if not isinstance(payload, dict):
            raise AdapterError("MCP HTTP 响应无法解析")
        if "error" in payload:
            raise AdapterError(payload["error"].get("message", "MCP 调用失败"))
        return payload.get("result")

    async def discover(self) -> list[ToolDescriptor]:
        result = await self._rpc("tools/list")
        return [
            ToolDescriptor(
                name=item["name"],
                description=item.get("description", ""),
                input_schema=item.get("inputSchema", {"type": "object"}),
                output_schema=item.get("outputSchema"),
                annotations=item.get("annotations") or {},
            )
            for item in result.get("tools", [])
        ]

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if result.get("isError"):
            raise AdapterError("MCP Tool 返回错误")
        return result


class StdioMCPGateway:
    def __init__(
        self,
        command: list[str],
        allowed_executables: set[str],
        environment: dict[str, str] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if not command or Path(command[0]).name not in allowed_executables:
            raise AdapterError("MCP STDIO 命令不在白名单")
        self.command = command
        self.environment = environment or {}
        self.timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def __aenter__(self) -> StdioMCPGateway:
        environment = {"PATH": os.environ.get("PATH", ""), **self.environment}
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "skillx-workflow", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized")
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except TimeoutError:
                self._process.kill()

    async def _write(self, payload: dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise AdapterError("MCP 进程未启动")
        self._process.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        )
        await self._process.stdin.drain()

    async def _notify(self, method: str) -> None:
        await self._write({"jsonrpc": "2.0", "method": method})

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if not self._process or not self._process.stdout:
            raise AdapterError("MCP 进程未启动")
        self._request_id += 1
        request_id = self._request_id
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=self.timeout)
            if not line:
                raise AdapterError("MCP 进程意外退出")
            payload = json.loads(line)
            if payload.get("id") != request_id:
                continue
            if "error" in payload:
                raise AdapterError(payload["error"].get("message", "MCP 调用失败"))
            return payload.get("result")

    async def discover(self) -> list[ToolDescriptor]:
        result = await self._rpc("tools/list")
        return [
            ToolDescriptor(
                name=item["name"],
                description=item.get("description", ""),
                input_schema=item.get("inputSchema", {"type": "object"}),
                output_schema=item.get("outputSchema"),
                annotations=item.get("annotations") or {},
            )
            for item in result.get("tools", [])
        ]

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if result.get("isError"):
            raise AdapterError("MCP Tool 返回错误")
        return result




class SubprocessAgentAdapter:
    def __init__(
        self, command: list[str], allowed_executables: set[str],
        runtime: AgentRuntimeConfig | None = None,
    ) -> None:
        if not command or Path(command[0]).name not in allowed_executables:
            raise AdapterError("Agent 命令不在白名单")
        self.command = command
        self._runtime = runtime
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    def extra_arguments(self, request: AgentRunRequest) -> list[str]:
        return []

    @contextmanager
    def request_arguments(self, request: AgentRunRequest):
        yield [
            *self.extra_arguments(request),
            "--prompt",
            request.prompt,
            "--max-steps",
            str(request.max_steps),
            "--max-tokens",
            str(request.max_tokens),
            "--allowed-tools",
            json.dumps(request.allowed_tools, ensure_ascii=False),
            "--output-schema",
            json.dumps(request.output_schema, ensure_ascii=False),
        ]

    async def events(self, run_id: str, request: AgentRunRequest) -> AsyncIterator[dict[str, Any]]:
        with self.request_arguments(request) as arguments:
            async for event in self._events(run_id, request, arguments):
                yield event

    async def _events(
        self, run_id: str, request: AgentRunRequest, arguments: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        working_directory = request.working_directory.resolve()
        working_directory.mkdir(parents=True, exist_ok=True)
        runtime = self._runtime or agent_runtime_config()
        provider_environment = {
            "SKILLX_AGENT_ENDPOINT": runtime.endpoint or "",
            "SKILLX_AGENT_MODEL": runtime.model or "",
            "SKILLX_PI_MODEL_OPTIONS": json.dumps(runtime.model_options),
            "SKILLX_REASONING_EFFORT": runtime.reasoning_effort or "",
            "SKILLX_AGENT_API_KEY_ENV": runtime.api_key_env,
            "SKILLX_MODEL_ENDPOINT": runtime.endpoint or "",
            "SKILLX_MODEL_NAME": runtime.model or "",
            "SKILLX_MODEL_API_KEY_ENV": runtime.api_key_env,
        }
        api_key = resolve_runtime_key(runtime.api_key_env)
        if api_key:
            provider_environment[runtime.api_key_env] = api_key
        process = await asyncio.create_subprocess_exec(
            *self.command,
            *arguments,
            cwd=working_directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), **provider_environment},
        )
        self._processes[run_id] = process
        if not process.stdout:
            raise AdapterError("Agent stdout 不可用")
        async def drain_stderr() -> None:
            if process.stderr is not None:
                while await process.stderr.read(4096):
                    pass

        stderr_task = asyncio.create_task(drain_stderr())
        try:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "log.emitted", "payload": {"message": line.decode().strip()}}
                yield event
            code = await process.wait()
            if code != 0:
                raise AdapterError(f"Agent 进程退出码: {code}")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            self._processes.pop(run_id, None)

    async def send_tool_result(
        self, run_id: str, result: dict[str, Any]
    ) -> None:
        """把 Host 产生的 ToolResult 回送给暂停中的同一 Agent 进程。"""

        process = self._processes.get(run_id)
        if process is None or process.returncode is not None or process.stdin is None:
            raise AdapterError("Agent stdin 不可用，无法回送 ToolResult")
        process.stdin.write(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )
        await process.stdin.drain()

    async def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


TraeXSubprocessAdapter = SubprocessAgentAdapter
