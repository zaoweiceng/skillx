from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx


class AdapterError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str
    working_directory: Path
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 8
    max_tokens: int = 30000


class TraeXSubprocessAdapter:
    def __init__(self, command: list[str], allowed_executables: set[str]) -> None:
        if not command or Path(command[0]).name not in allowed_executables:
            raise AdapterError("TraeX 命令不在白名单")
        self.command = command
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def events(self, run_id: str, request: AgentRunRequest) -> AsyncIterator[dict[str, Any]]:
        working_directory = request.working_directory.resolve()
        working_directory.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *self.command,
            "--prompt",
            request.prompt,
            "--max-steps",
            str(request.max_steps),
            "--max-tokens",
            str(request.max_tokens),
            "--allowed-tools",
            json.dumps(request.allowed_tools, ensure_ascii=False),
            cwd=working_directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},
        )
        self._processes[run_id] = process
        if not process.stdout:
            raise AdapterError("TraeX stdout 不可用")
        try:
            async for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "log.emitted", "payload": {"message": line.decode().strip()}}
                yield event
            code = await process.wait()
            if code != 0:
                raise AdapterError(f"TraeX 进程退出码: {code}")
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
            self._processes.pop(run_id, None)

    async def cancel(self, run_id: str) -> None:
        process = self._processes.get(run_id)
        if not process or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
