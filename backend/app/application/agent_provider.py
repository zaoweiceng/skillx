from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.runtime.models import OpenAICompatibleAdapter


def _protocol_available() -> tuple[bool, str]:
    settings = get_settings()
    command = settings.traex_command
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
    if executable.name not in settings.traex_allowed_executables:
        return False, f"Agent executable 未在 allowlist: {executable.name}"
    return True, "Host 双向 subprocess 协议可启动"


async def provider_readiness() -> dict[str, Any]:
    settings = get_settings()
    provider_type = settings.agent_provider_type
    protocol_available, protocol_summary = _protocol_available()
    model_available = False
    probe_summary = protocol_summary
    if provider_type == "real" and protocol_available:
        key_configured = bool(os.environ.get(settings.model_api_key_env))
        if not settings.model_endpoint or not settings.model_name or not key_configured:
            probe_summary = "真实模型 endpoint、model 或 API key 未完整配置"
        else:
            try:
                result = await OpenAICompatibleAdapter().generate(
                    "Return {\"ok\": true}.",
                    {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                    16,
                )
                model_available = result.output.get("ok") is True
                probe_summary = f"真实模型探测成功: {result.model}"
            except Exception as exc:  # noqa: BLE001 - readiness returns the fact.
                probe_summary = f"真实模型探测失败: {exc}"
    elif provider_type == "fixture":
        probe_summary = (
            "协议 fixture 可用，仅用于协议/故障测试"
            if protocol_available
            else protocol_summary
        )
    else:
        probe_summary = "真实语义 Agent 未配置"
    return {
        "type": provider_type,
        "configured": provider_type != "unavailable" and bool(settings.traex_command),
        "protocolAvailable": protocol_available,
        "modelAvailable": model_available,
        "lastProbe": {
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "ok": (
                model_available
                if provider_type == "real"
                else protocol_available
                if provider_type == "fixture"
                else False
            ),
            "summary": probe_summary,
        },
    }
