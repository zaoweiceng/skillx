from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.runtime_config import agent_runtime_config, llm_runtime_config
from app.config import get_settings

_last_probe: dict[str, Any] | None = None
_next_probe_at: datetime | None = None
_probe_expires_at: datetime | None = None


def _probe_fingerprints() -> tuple[str, str]:
    runtime = agent_runtime_config()
    command = list(runtime.command)
    sidecar = hashlib.sha256()
    sidecar.update(json.dumps(command, separators=(",", ":")).encode())
    for argument in command[1:]:
        path = Path(argument)
        if path.is_file():
            try:
                sidecar.update(path.read_bytes())
            except OSError:
                sidecar.update(b"unreadable-sidecar")
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
    return True, "Host 双向 subprocess 协议可启动"


async def provider_readiness() -> dict[str, Any]:
    runtime = agent_runtime_config()
    provider_type = (
        "fixture"
        if runtime.type == "fixture"
        else "real"
        if runtime.type in {"traex_managed", "openai_sidecar"}
        else "unavailable"
    )
    protocol_available, protocol_summary = _protocol_available()
    configured = runtime.configured
    now = datetime.now(UTC)
    config_digest, _sidecar_digest = _probe_fingerprints()
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
        elif runtime.type == "openai_sidecar" and (
            not runtime.endpoint or not runtime.model or not key_configured
        ):
            summary = "Agent sidecar endpoint、model 或 API key 未完整配置"
        elif runtime.type == "traex_managed":
            summary = "TraeX managed Agent 协议已配置，不要求 Host 持有 LLM endpoint"
        elif _last_probe is None:
            summary = "Agent Runtime 已配置，尚未执行同路径主动探测"
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
        summary = "真实语义 Agent 未配置"
    model_available = bool(
        provider_type == "real"
        and (
            (runtime.type == "traex_managed" and protocol_available)
            or (cache_valid and _last_probe and _last_probe.get("ok"))
        )
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
    return {
        "type": provider_type,
        "runtimeType": runtime.type,
        "configured": configured,
        "protocolAvailable": protocol_available,
        "modelAvailable": model_available,
        "model": runtime.model,
        "digest": runtime.digest,
        "source": runtime.source,
        "lastProbe": last_probe,
        "nextProbeAllowedAt": (
            _next_probe_at.isoformat().replace("+00:00", "Z")
            if _next_probe_at and _next_probe_at > now
            else None
        ),
    }


async def probe_provider() -> dict[str, Any]:
    """Explicitly probe the same sidecar, function-call and Host ToolResult path."""

    global _last_probe, _next_probe_at, _probe_expires_at

    settings = get_settings()
    runtime = agent_runtime_config()
    now = datetime.now(UTC)
    config_digest, sidecar_digest = _probe_fingerprints()
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
    if runtime.type not in {"traex_managed", "openai_sidecar"}:
        raise ValueError("仅 real Provider 支持付费主动探测")
    if not protocol_available:
        raise ValueError(protocol_summary)
    if runtime.type == "openai_sidecar" and not runtime.configured:
        raise ValueError("Agent sidecar endpoint、model 或 API key 未完整配置")
    _next_probe_at = now + timedelta(seconds=settings.provider_probe_cooldown_seconds)
    _probe_expires_at = now + timedelta(seconds=settings.provider_probe_ttl_seconds)
    probe_id = str(uuid4())
    working_directory = settings.artifact_root.resolve()
    working_directory.mkdir(parents=True, exist_ok=True)
    try:
        result = await AgentHost().execute(
            AgentHostRequest(
                run_id=f"provider-probe-{probe_id}",
                prompt=(
                    "这是运行时兼容性探测。必须调用 lookup.table，参数 key=q1；"
                    "收到 Host ToolResult 后，把 value 原样作为 reply 返回。"
                ),
                output_schema={
                    "type": "object",
                    "properties": {
                        "reply": {
                            "type": "string",
                            "const": "第一季度：营收增长 12%",
                        }
                    },
                    "required": ["reply"],
                    "additionalProperties": False,
                },
                working_directory=working_directory,
                requested_capabilities=("lookup.table",),
                max_steps=3,
                max_total_tokens=2000,
                max_duration_seconds=settings.provider_probe_timeout_seconds,
            )
        )
        tool_round_trip = {
            item["type"] for item in result.runtime_events
        }.issuperset({"tool.requested", "tool.result"})
        ok = result.output == {"reply": "第一季度：营收增长 12%"} and tool_round_trip
        _last_probe = {
            "at": now.isoformat().replace("+00:00", "Z"),
            "ok": ok,
            "summary": (
                "同一 sidecar/function calling/Host ToolResult/strict output 探测成功"
                if ok
                else "Provider 未完成 Host ToolResult round-trip"
            ),
            "kind": "sidecar_host_round_trip",
            "probeId": probe_id,
            "model": runtime.model,
            "configDigest": config_digest,
            "sidecarDigest": sidecar_digest,
            "totalTokens": result.total_tokens,
            "usageComplete": result.usage_complete,
        }
    except AgentHostError as exc:
        _last_probe = {
            "at": now.isoformat().replace("+00:00", "Z"),
            "ok": False,
            "summary": f"Provider 兼容性探测失败: {exc.code}",
            "kind": "sidecar_host_round_trip",
            "probeId": probe_id,
            "model": runtime.model,
            "configDigest": config_digest,
            "sidecarDigest": sidecar_digest,
            "code": exc.code,
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
