from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.runtime_config import agent_runtime_config, llm_runtime_config
from app.application.tool_broker import ToolAdapterRegistry, ToolExecutionContext
from app.config import get_settings

_last_probe: dict[str, Any] | None = None
_next_probe_at: datetime | None = None
_probe_expires_at: datetime | None = None


def _probe_fingerprints() -> tuple[str, str]:
    runtime = agent_runtime_config()
    if runtime.adapter_digest:
        sidecar_digest = runtime.adapter_digest
        config = {
            "type": runtime.type,
            "endpoint": runtime.endpoint,
            "model": runtime.model,
            "sidecarDigest": sidecar_digest,
        }
        config_digest = "sha256:" + hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return config_digest, sidecar_digest
    command = list(runtime.command)
    sidecar = hashlib.sha256()
    adapter_paths = [Path(argument) for argument in command[1:] if Path(argument).is_file()]
    if not adapter_paths and command:
        resolved = shutil.which(command[0])
        executable = Path(resolved) if resolved else Path(command[0])
        if executable.is_file():
            adapter_paths = [executable]
    if adapter_paths:
        for path in adapter_paths:
            try:
                sidecar.update(path.read_bytes())
            except OSError:
                sidecar.update(b"unreadable-adapter")
    else:
        sidecar.update(json.dumps(command, separators=(",", ":")).encode())
    sidecar_digest = f"sha256:{sidecar.hexdigest()}"
    config = {
        "type": runtime.type,
        "endpoint": runtime.endpoint,
        "model": runtime.model,
        "sidecarDigest": sidecar_digest,
    }
    config_digest = "sha256:" + hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return config_digest, sidecar_digest


def _adapter_trust(sidecar_digest: str) -> tuple[bool, str]:
    runtime = agent_runtime_config()
    if runtime.type == "traex_builtin" and sidecar_digest == runtime.adapter_digest:
        return True, runtime.adapter_version
    configured = set(get_settings().agent_trusted_adapter_digests)
    bundled = Path(__file__).resolve().parents[3] / "examples" / "openai_agent_sidecar.py"
    bundled_digest = (
        "sha256:" + hashlib.sha256(bundled.read_bytes()).hexdigest()
        if bundled.is_file()
        else None
    )
    if runtime.type == "openai_sidecar" and sidecar_digest == bundled_digest:
        return True, "openai-agent-sidecar/v1"
    if sidecar_digest in configured:
        return True, f"{runtime.type}/configured"
    return False, f"{runtime.type}/untrusted"


class _ProbeLookupAdapter:
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
    fixture_policy = "do_not_capture"

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    async def execute(
        self, _context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if arguments["key"] != self.key:
            return {"table": {self.key: self.value}, "value": "challenge-key-mismatch"}
        return {"table": {self.key: self.value}, "value": self.value}


def _protocol_available() -> tuple[bool, str]:
    runtime = agent_runtime_config()
    command = runtime.command
    if not command:
        return False, "未配置 Agent command"
    executable = Path(command[0])
    resolved = (
        executable
        if executable.is_absolute() and executable.exists()
        else shutil.which(command[0])
    )
    if resolved is None:
        return False, f"Agent executable 不存在: {command[0]}"
    if executable.name not in runtime.allowed_executables:
        return False, f"Agent executable 未在 allowlist: {executable.name}"
    return True, (
        "原生 TraeX JSONL + 本轮 loopback MCP bridge 可启动"
        if runtime.type == "traex_builtin"
        else "Host 双向 subprocess 协议可启动"
    )


async def _native_login_status() -> tuple[str, str]:
    runtime = agent_runtime_config()
    if runtime.type != "traex_builtin" or not runtime.command:
        return "not_applicable", ""
    try:
        process = await asyncio.create_subprocess_exec(
            runtime.command[0],
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "HOME",
                    "PATH",
                    "TMPDIR",
                    "LANG",
                    "LC_ALL",
                    "CODEX_HOME",
                    "TRAE_HOME",
                }
            },
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
            return "unknown", "无法确认 TraeX 登录状态"
    except OSError:
        return "unknown", "无法确认 TraeX 登录状态"
    text = stdout.decode(errors="replace").strip()
    if process.returncode == 0 and "logged in" in text.lower():
        # 登录状态只证明本地存在凭据，不代表 Runtime 可用；usable 由主动探测单独判定。
        return (
            "credential_present_unverified",
            "TraeX 本地凭据已存在（未验证），需执行兼容性探测确认可用",
        )
    return "auth_required", "请在终端运行 traex login 完成本机登录"


async def provider_readiness() -> dict[str, Any]:
    settings = get_settings()
    runtime = agent_runtime_config()
    provider_type = (
        "fixture"
        if runtime.type == "fixture"
        else "real"
        if runtime.type in {"traex_builtin", "traex_managed", "openai_sidecar"}
        else "unavailable"
    )
    protocol_available, protocol_summary = _protocol_available()
    configured = runtime.configured
    now = datetime.now(UTC)
    config_digest, sidecar_digest = _probe_fingerprints()
    adapter_trusted, adapter_version = _adapter_trust(sidecar_digest)
    auth_status, auth_summary = await _native_login_status()
    cache_valid = bool(
        _last_probe
        and _probe_expires_at
        and now < _probe_expires_at
        and _last_probe.get("configDigest") == config_digest
    )
    summary = protocol_summary
    if provider_type == "real":
        key_configured = bool(os.environ.get(runtime.api_key_env))
        if not protocol_available:
            summary = protocol_summary
        elif runtime.type == "traex_builtin" and auth_status == "auth_required":
            summary = auth_summary
        elif runtime.type == "openai_sidecar" and (
            not runtime.endpoint or not runtime.model or not key_configured
        ):
            summary = "Agent sidecar endpoint、model 或 API key 未完整配置"
        elif not adapter_trusted:
            summary = "Agent adapter 可执行，但 digest 未受信，语义选择保持不可用"
        elif _last_probe is None:
            summary = (
                "内置 TraeX Runtime 已自动发现；需完成 Host ToolResult 能力探测后"
                "才开放语义选择"
                if runtime.type == "traex_builtin"
                else "TraeX managed Agent 协议已配置；需完成 Host ToolResult 能力探测后"
                "才开放语义选择"
                if runtime.type == "traex_managed"
                else "Agent Runtime 已配置，尚未执行同路径主动探测"
            )
        elif not cache_valid:
            summary = "最近一次真实模型探测已过期或配置已变化，请重新探测"
        else:
            summary = str(_last_probe["summary"])
    elif provider_type == "fixture":
        summary = (
            "协议 fixture 可用，仅用于协议/故障测试"
            if protocol_available
            else protocol_summary
        )
    else:
        summary = (
            "PATH 中未发现 TraeX，请先安装并运行 traex login"
            if runtime.source == "auto_discovery_missing"
            else "真实语义 Agent 未配置"
        )
    model_available = bool(
        provider_type == "real"
        and cache_valid
        and adapter_trusted
        and _last_probe
        and _last_probe.get("ok")
        and _last_probe.get("adapterTrusted")
    )
    last_probe = (
        {
            **_last_probe,
            "stale": not cache_valid,
            "expiresAt": (
                _probe_expires_at.isoformat().replace("+00:00", "Z")
                if _probe_expires_at
                else None
            ),
            **({"summary": summary} if not cache_valid else {}),
        }
        if provider_type == "real" and _last_probe is not None
        else {
            "at": None,
            "ok": protocol_available if provider_type == "fixture" else False,
            "summary": summary,
            "kind": "protocol_only" if provider_type == "fixture" else "not_run",
        }
    )
    # 分离展示与持久化的运行时事实：installed（已安装且协议可启动）、
    # credentialPresent（本地凭据存在但未验证）、activeProbeAuthenticated（主动探测已认证）、
    # protocolCompatible（strict output + ToolResult round-trip 通过）、usable（新鲜且可用）。
    # 只有真实主动探测成功且未过期才标记 usable，login 状态本身绝不代表 usable。
    capability_states = {
        "installed": provider_type == "real" and protocol_available,
        "credentialPresent": provider_type == "real"
        and auth_status == "credential_present_unverified",
        "activeProbeAuthenticated": provider_type == "real"
        and cache_valid
        and bool(_last_probe and _last_probe.get("activeAuthenticated")),
        "modelReachable": provider_type == "real"
        and bool(_last_probe and _last_probe.get("modelReachable")),
        "protocolCompatible": provider_type == "real"
        and bool(_last_probe and _last_probe.get("protocolCompatible")),
        "toolRoundTrip": provider_type == "real"
        and bool(_last_probe and _last_probe.get("toolRoundTrip")),
        "usable": model_available,
    }
    return {
        "type": provider_type,
        "runtimeType": runtime.type,
        "protocol": runtime.protocol,
        "provider": runtime.provider,
        "authStatus": auth_status,
        "capabilityStates": capability_states,
        "protocolTestOnly": provider_type == "fixture",
        "configured": configured,
        "protocolAvailable": protocol_available,
        "modelAvailable": model_available,
        "model": runtime.model,
        "digest": runtime.digest,
        "adapterDigest": sidecar_digest,
        "adapterVersion": adapter_version,
        "adapterTrusted": adapter_trusted,
        "source": runtime.source,
        "lastProbe": last_probe,
        "nextProbeAllowedAt": (
            _next_probe_at.isoformat().replace("+00:00", "Z")
            if _next_probe_at and _next_probe_at > now
            else None
        ),
        "probeBudgetProfile": {
            "estimatedInputTokens": settings.provider_probe_estimated_input_tokens,
            "maxInputTokens": settings.provider_probe_max_input_tokens,
            "maxOutputTokens": settings.provider_probe_max_output_tokens,
            "maxBillableTokens": settings.provider_probe_max_billable_tokens,
        },
        "workflowSelectionBudgetProfile": {
            "maxSteps": settings.agent_max_steps,
            "maxInputTokens": settings.agent_max_input_tokens,
            "maxOutputTokens": settings.agent_max_output_tokens,
            "maxBillableTokens": settings.agent_max_billable_tokens,
            "catalogDescriptors": 1,
        },
    }


async def probe_provider() -> dict[str, Any]:
    """Explicitly probe the same sidecar, function-call and Host ToolResult path."""

    global _last_probe, _next_probe_at, _probe_expires_at

    settings = get_settings()
    runtime = agent_runtime_config()
    now = datetime.now(UTC)
    config_digest, sidecar_digest = _probe_fingerprints()
    adapter_trusted, adapter_version = _adapter_trust(sidecar_digest)
    if (
        _next_probe_at is not None
        and now < _next_probe_at
        and _last_probe is not None
        and _last_probe.get("configDigest") == config_digest
    ):
        return {
            **(_last_probe or {}),
            "code": "PROBE_RATE_LIMITED",
            "nextProbeAllowedAt": _next_probe_at.isoformat().replace("+00:00", "Z"),
        }
    protocol_available, protocol_summary = _protocol_available()
    if runtime.type not in {"traex_builtin", "traex_managed", "openai_sidecar"}:
        raise ValueError("仅 real Provider 支持付费主动探测")
    if not protocol_available:
        raise ValueError(protocol_summary)
    if runtime.type == "openai_sidecar" and not runtime.configured:
        raise ValueError("Agent sidecar endpoint、model 或 API key 未完整配置")
    if not adapter_trusted:
        raise ValueError("Agent adapter digest 未在可信列表，不能执行付费语义探测")
    _next_probe_at = now + timedelta(seconds=settings.provider_probe_cooldown_seconds)
    _probe_expires_at = now + timedelta(seconds=settings.provider_probe_ttl_seconds)
    probe_id = str(uuid4())
    working_directory = settings.artifact_root.resolve()
    working_directory.mkdir(parents=True, exist_ok=True)
    challenge_key = f"probe-{secrets.token_hex(8)}"
    challenge_value = f"challenge-{secrets.token_urlsafe(18)}"
    try:
        result = await AgentHost(
            tool_registry=ToolAdapterRegistry(
                [_ProbeLookupAdapter(challenge_key, challenge_value)]
            )
        ).execute(
            AgentHostRequest(
                run_id=f"provider-probe-{probe_id}",
                prompt=(
                    "这是运行时兼容性探测。必须调用 lookup.table，参数 "
                    f"key={challenge_key}；"
                    "收到 Host ToolResult 后，把 value 原样作为 reply 返回。"
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "reply": {
                            "type": "string",
                            "const": challenge_value,
                        }
                    },
                    "required": ["reply"],
                    "additionalProperties": False,
                },
                working_directory=working_directory,
                requested_capabilities=("lookup.table",),
                max_steps=3,
                max_total_tokens=settings.provider_probe_max_billable_tokens,
                max_input_tokens=settings.provider_probe_max_input_tokens,
                max_output_tokens=settings.provider_probe_max_output_tokens,
                max_billable_tokens=settings.provider_probe_max_billable_tokens,
                max_duration_seconds=settings.provider_probe_timeout_seconds,
            )
        )
        tool_round_trip = {
            item["type"] for item in result.runtime_events
        }.issuperset({"tool.requested", "tool.result"})
        explicit_dispatch = any(
            item["type"] == "model.request.dispatched"
            for item in result.runtime_events
        )
        real_usage = result.usage_complete and result.total_tokens > 0
        ok = (
            result.output == {"reply": challenge_value}
            and tool_round_trip
            and explicit_dispatch
            and result.request_dispatched
            and real_usage
        )
        _last_probe = {
            "at": now.isoformat().replace("+00:00", "Z"),
            "ok": ok,
            "summary": (
                "同一 sidecar/function calling/Host ToolResult/strict output 探测成功"
                if ok
                else "Provider 未完成显式 dispatch、真实 Usage 或 ToolResult round-trip"
            ),
            "kind": "sidecar_host_round_trip",
            "probeId": probe_id,
            "model": result.model,
            "provider": runtime.provider,
            "protocol": runtime.protocol,
            "runtimeType": runtime.type,
            "configDigest": config_digest,
            "sidecarDigest": sidecar_digest,
            "adapterVersion": adapter_version,
            "adapterTrusted": adapter_trusted,
            "challenge": challenge_key,
            "inputTokens": result.input_tokens,
            "outputTokens": result.output_tokens,
            "totalTokens": result.total_tokens,
            "usageComplete": result.usage_complete,
            "requestDispatched": result.request_dispatched,
            "activeAuthenticated": True,
            "modelReachable": True,
            "protocolCompatible": ok,
            "toolRoundTrip": tool_round_trip,
            "budgetProfile": {
                "maxInputTokens": settings.provider_probe_max_input_tokens,
                "maxOutputTokens": settings.provider_probe_max_output_tokens,
                "maxBillableTokens": settings.provider_probe_max_billable_tokens,
            },
            "completed": True,
        }
    except AgentHostError as exc:
        _last_probe = {
            "at": now.isoformat().replace("+00:00", "Z"),
            "ok": False,
            "summary": f"Provider 兼容性探测失败: {exc.code}",
            "kind": "sidecar_host_round_trip",
            "probeId": probe_id,
            "model": runtime.model,
            "provider": runtime.provider,
            "protocol": runtime.protocol,
            "runtimeType": runtime.type,
            "configDigest": config_digest,
            "sidecarDigest": sidecar_digest,
            "adapterVersion": adapter_version,
            "adapterTrusted": adapter_trusted,
            "challenge": challenge_key,
            "code": exc.code,
            "inputTokens": exc.input_tokens or None,
            "outputTokens": exc.output_tokens or None,
            "totalTokens": exc.total_tokens or None,
            "usageComplete": exc.usage_complete,
            "requestDispatched": exc.request_dispatched,
            "activeAuthenticated": exc.code != "TRAE_AUTH_REQUIRED"
            and exc.request_dispatched,
            "modelReachable": exc.request_dispatched or exc.total_tokens > 0,
            "protocolCompatible": False,
            "toolRoundTrip": False,
            "budgetProfile": {
                "maxInputTokens": settings.provider_probe_max_input_tokens,
                "maxOutputTokens": settings.provider_probe_max_output_tokens,
                "maxBillableTokens": settings.provider_probe_max_billable_tokens,
            },
            "completed": False,
        }
    return {
        **_last_probe,
        "nextProbeAllowedAt": _next_probe_at.isoformat().replace("+00:00", "Z"),
    }


def reset_provider_probe_cache() -> None:
    """Test helper; production cache is process-local and intentionally bounded."""

    global _last_probe, _next_probe_at, _probe_expires_at
    _last_probe = None
    _next_probe_at = None
    _probe_expires_at = None


async def llm_runtime_readiness() -> dict[str, Any]:
    runtime = llm_runtime_config()
    configured = runtime.configured
    return {
        "type": runtime.type,
        "configured": configured,
        "protocol": (
            "openai-compatible-http"
            if runtime.type == "openai_compatible"
            else "none"
        ),
        "model": runtime.model,
        "probe": {
            "kind": "configuration_only",
            "ok": configured,
            "summary": (
                "Direct LLM Runtime 已配置；执行中 401/429/timeout 不会切换 Runtime"
                if configured
                else "Direct LLM Runtime 未配置，可在 Run 启动前使用 Agent fallback"
            ),
        },
        "digest": runtime.digest,
        "source": runtime.source,
    }
