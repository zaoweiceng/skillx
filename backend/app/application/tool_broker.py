from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from jsonschema import Draft202012Validator

from app.config import get_settings


class ToolBrokerError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    call_id: str
    working_directory: Path


class ToolAdapter(Protocol):
    capability_ref: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect: str
    idempotent: bool
    fixture_policy: str

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...


def evidence_digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ToolAdapterRegistry:
    def __init__(self, adapters: list[ToolAdapter] | None = None) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        defaults: list[ToolAdapter] = [
            BrowserReadAdapter(),
            LocalReadAdapter(),
            LookupTableAdapter(),
            SummarizeTextAdapter(),
        ]
        for adapter in defaults if adapters is None else adapters:
            self.register(adapter)

    def register(self, adapter: ToolAdapter) -> None:
        if adapter.capability_ref in self._adapters:
            raise ValueError(f"重复 Tool Adapter: {adapter.capability_ref}")
        self._adapters[adapter.capability_ref] = adapter

    def describe(self, capability_ref: str) -> dict[str, Any]:
        adapter = self._adapters.get(capability_ref)
        if adapter is None:
            raise ToolBrokerError(
                "CAPABILITY_NOT_REGISTERED",
                f"能力 {capability_ref} 未注册 Host Adapter",
            )
        descriptor = {
            "capabilityRef": adapter.capability_ref,
            "inputSchema": adapter.input_schema,
            "outputSchema": adapter.output_schema,
            "effect": adapter.effect,
            "idempotent": adapter.idempotent,
            "fixturePolicy": adapter.fixture_policy,
            "actionKind": getattr(adapter, "action_kind", "tool"),
            "replayability": getattr(adapter, "replayability", "agent_only"),
        }
        descriptor["capabilityIdentity"] = evidence_digest(descriptor)
        return descriptor

    async def execute(
        self,
        capability_ref: str,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        adapter = self._adapters.get(capability_ref)
        if adapter is None:
            raise ToolBrokerError(
                "CAPABILITY_NOT_REGISTERED",
                f"能力 {capability_ref} 未注册 Host Adapter",
            )
        input_errors = list(
            Draft202012Validator(adapter.input_schema).iter_errors(arguments)
        )
        if input_errors:
            recorder = getattr(adapter, "record_input_failure", None)
            if recorder is not None:
                await recorder(context, arguments, "INVALID_TOOL_ARGUMENTS")
            raise ToolBrokerError(
                "INVALID_TOOL_ARGUMENTS",
                f"{capability_ref} 参数不符合契约: {input_errors[0].message}",
            )
        try:
            output = await adapter.execute(context, arguments)
        except ToolBrokerError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ToolBrokerError(
                "TIMEOUT",
                f"{capability_ref} 执行超时",
            ) from exc
        except (ConnectionError, OSError, httpx.TransportError) as exc:
            raise ToolBrokerError(
                "DEPENDENCY_UNAVAILABLE",
                f"{capability_ref} 连接外部依赖失败",
            ) from exc
        except Exception as exc:
            raise ToolBrokerError(
                "TOOL_EXECUTION_FAILED",
                f"{capability_ref} 执行失败: {exc}",
            ) from exc
        output_errors = list(
            Draft202012Validator(adapter.output_schema).iter_errors(output)
        )
        if output_errors:
            raise ToolBrokerError(
                "TOOL_CONTRACT_VIOLATION",
                f"{capability_ref} 输出不符合契约: {output_errors[0].message}",
            )
        return output

    def distillation_evidence(
        self, capability_ref: str, output: dict[str, Any]
    ) -> dict[str, Any]:
        adapter = self._adapters.get(capability_ref)
        evidence = getattr(adapter, "distillation_evidence", None)
        return evidence(output) if callable(evidence) else {}


class BrowserReadAdapter:
    capability_ref = "browser.read"
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 1},
            "pageContract": {"type": "object"},
        },
        "required": ["url", "pageContract"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            "fields": {"type": "object"},
        },
        "required": ["url", "title", "content", "fields"],
        "additionalProperties": True,
    }
    effect = "none"
    idempotent = True
    fixture_policy = "capture_redacted"
    action_kind = "browser"
    replayability = "parameterizable"

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        from app.domain.workflow import NodeType, WorkflowNode
        from app.runtime.executors import (
            CancellationToken,
            ExecutorRegistry,
            RunContext,
        )

        url = arguments["url"]
        self._validate_url(url)
        node = WorkflowNode(
            id="broker_browser_read",
            type=NodeType.BROWSER,
            displayName="Broker Browser Read",
            input={"url": url},
            config={
                "action": "read",
                "runtime": "playwright",
                "pageContract": arguments["pageContract"],
            },
        )
        run_context = RunContext(
            run_id=context.run_id,
            run_input={"url": url},
            node_run_id=f"{context.run_id}:browser:{context.call_id}",
            cancellation=CancellationToken(asyncio.Event()),
        )
        result = await ExecutorRegistry().get(NodeType.BROWSER).execute(
            run_context, node, {"url": url}
        )
        if result.status != "succeeded" or result.output is None:
            raise ToolBrokerError(
                result.error.code if result.error else "EXECUTION_FAILED",
                result.error.message if result.error else "browser.read 执行失败",
            )
        return result.output

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or not hostname
        ):
            raise ToolBrokerError("PERMISSION_DENIED", "browser.read URL 不安全")
        allowed = set(get_settings().browser_allowed_hosts)
        if hostname == "localhost" and hostname not in allowed:
            raise ToolBrokerError(
                "PERMISSION_DENIED", "browser.read 本机目标未在 Host allowlist"
            )
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if (
            address is not None
            and (address.is_private or address.is_loopback or address.is_link_local)
            and hostname not in allowed
        ):
            raise ToolBrokerError(
                "PERMISSION_DENIED", "browser.read 私网目标未在 Host allowlist"
            )
        if address is not None:
            return
        try:
            resolved = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ToolBrokerError(
                "DEPENDENCY_UNAVAILABLE", "browser.read 目标域名无法解析"
            ) from exc
        for item in resolved:
            resolved_address = ipaddress.ip_address(item[4][0])
            if (
                resolved_address.is_private
                or resolved_address.is_loopback
                or resolved_address.is_link_local
            ) and hostname not in allowed:
                raise ToolBrokerError(
                    "PERMISSION_DENIED", "browser.read 域名解析到未授权私网地址"
                )


class LocalReadAdapter:
    capability_ref = "read.local"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "minLength": 1}},
        "required": ["path"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "bytes": {"type": "integer", "minimum": 0},
        },
        "required": ["path", "content", "bytes"],
        "additionalProperties": False,
    }
    effect = "none"
    idempotent = True
    fixture_policy = "capture_redacted"
    action_kind = "file"
    replayability = "parameterizable"

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        root = context.working_directory.resolve()
        target = (root / arguments["path"]).resolve()
        if not target.is_relative_to(root):
            raise ToolBrokerError(
                "PERMISSION_DENIED", "read.local 只能读取任务工作目录内的普通文件"
            )
        if target.is_dir():
            raise ToolBrokerError("INVALID_INPUT", "目标是目录，请使用代码列出目录，再读取具体文件")
        if not target.is_file():
            raise ToolBrokerError("NOT_FOUND", "目标文件不存在或不是普通文件")
        content = await asyncio.to_thread(target.read_bytes)
        if len(content) > 64 * 1024:
            raise ToolBrokerError("PAYLOAD_TOO_LARGE", "read.local 文件超过 64 KiB")
        try:
            text = content.decode()
        except UnicodeDecodeError as exc:
            raise ToolBrokerError("INVALID_ENCODING", "read.local 只支持 UTF-8 文本") from exc
        return {"path": arguments["path"], "content": text, "bytes": len(content)}


class LookupTableAdapter:
    capability_ref = "lookup.table"
    input_schema = {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {
            "table": {"type": "object"},
            "value": {"type": "string"},
        },
        "required": ["table", "value"],
        "additionalProperties": False,
    }
    effect = "none"
    idempotent = True
    fixture_policy = "capture_redacted"
    action_kind = "transform"
    replayability = "deterministic"
    table = {
        "q1": "第一季度：营收增长 12%",
        "q2": "第二季度：营收增长 8%",
        "q3": "第三季度：营收持平",
        "q4": "第四季度：营收增长 15%",
    }
    calls_by_key: dict[str, int] = {}

    async def execute(
        self, _context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        key = arguments["key"]
        self.calls_by_key[key] = self.calls_by_key.get(key, 0) + 1
        return {
            "table": dict(self.table),
            "value": self.table.get(key, "未知季度"),
        }

    @staticmethod
    def distillation_evidence(output: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation": "lookup",
            "table": output.get("table", {}),
            "resultSource": "host_tool_broker",
        }


class SummarizeTextAdapter:
    """Local Demo 的受控文本转换 Adapter。

    生产环境可替换为锁定模型的生成节点；恢复协议只依赖其 Host-owned ToolResult。
    """

    capability_ref = "summarize.text"
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    effect = "none"
    idempotent = True
    fixture_policy = "capture_redacted"

    async def execute(
        self, _context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return {"reply": arguments["text"]}
