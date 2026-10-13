from __future__ import annotations

import hashlib
import logging
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.domain.runtime import AgentRuntimeConfig as AgentRuntimeConfig
from app.domain.runtime import LLMRuntimeConfig as LLMRuntimeConfig

logger = logging.getLogger(__name__)
_legacy_logged = False


def adapter_source_digest(path: Path) -> str:
    content = hashlib.sha256(path.read_bytes())
    if path.name == "bridge.mjs" and path.parent.name == "pi":
        content.update(path.with_name("result-tools.mjs").read_bytes())
        content.update(path.with_name("openai-usage.mjs").read_bytes())
    return "sha256:" + content.hexdigest()


_agent_override: ContextVar[AgentRuntimeConfig | None] = ContextVar("agent_runtime", default=None)


@contextmanager
def agent_runtime_scope(runtime: AgentRuntimeConfig | None):
    token = _agent_override.set(runtime)
    try:
        yield
    finally:
        _agent_override.reset(token)


def _log_legacy_mapping() -> None:
    global _legacy_logged
    if _legacy_logged:
        return
    _legacy_logged = True
    logger.warning(
        "SKILLX_AGENT_PROVIDER_TYPE/SKILLX_TRAEX_* and SKILLX_MODEL_* are "
        "deprecated; legacy model settings map only to openai_sidecar, never "
        "to the Direct LLM Runtime"
    )


def llm_runtime_config() -> LLMRuntimeConfig:
    settings = get_settings()
    return LLMRuntimeConfig(
        type=settings.llm_runtime_type,
        endpoint=settings.llm_endpoint,
        model=settings.llm_model,
        api_key_env=settings.llm_api_key_env,
    )


def agent_runtime_config() -> AgentRuntimeConfig:
    if _agent_override.get() is not None:
        return _agent_override.get()
    settings = get_settings()
    if settings.agent_runtime_type != "unavailable" or settings.agent_command is not None:
        runtime_type = settings.agent_runtime_type
        command = tuple(settings.agent_command or ())
        if runtime_type == "pi" and not command:
            bridge = Path(__file__).resolve().parents[3] / "bridges/pi/bridge.mjs"
            command = (shutil.which("node") or "node", str(bridge))
        adapter_digest = ""
        if command:
            paths = [Path(item) for item in command[1:] if Path(item).is_file()]
            if paths:
                content = hashlib.sha256()
                for path in paths:
                    content.update(path.read_bytes())
                    if path.name == "bridge.mjs" and path.parent.name == "pi":
                        content.update(path.with_name("result-tools.mjs").read_bytes())
                        content.update(path.with_name("openai-usage.mjs").read_bytes())
                adapter_digest = f"sha256:{content.hexdigest()}"
        return AgentRuntimeConfig(
            type=runtime_type,
            command=command,
            allowed_executables=(
                ("node",) if runtime_type == "pi" else tuple(settings.agent_allowed_executables)
            ),
            endpoint=settings.agent_endpoint,
            model=settings.agent_model,
            api_key_env=settings.agent_api_key_env,
            protocol=(
                "openai-compatible-sidecar"
                if runtime_type == "openai_sidecar"
                else "host-ndjson"
            ),
            provider=(
                "openai-compatible"
                if runtime_type == "openai_sidecar"
                else "fixture"
                if runtime_type == "fixture"
                else "pi" if runtime_type == "pi" else "traex"
            ),
            adapter_version=f"{runtime_type}/configured",
            adapter_digest=adapter_digest,
        )

    if settings.agent_provider_type != "unavailable" or settings.traex_command:
        _log_legacy_mapping()
        runtime_type: Literal[
            "pi",
        "traex_builtin",
            "traex_managed",
            "openai_sidecar",
            "fixture",
            "unavailable",
        ] = {
            "real": "openai_sidecar",
            "fixture": "fixture",
            "unavailable": "unavailable",
        }[settings.agent_provider_type]
        command = tuple(settings.traex_command or ())
        paths = [Path(item) for item in command[1:] if Path(item).is_file()]
        content = hashlib.sha256()
        for path in paths:
            content.update(path.read_bytes())
        return AgentRuntimeConfig(
            type=runtime_type,
            command=command,
            allowed_executables=tuple(settings.traex_allowed_executables),
            endpoint=settings.model_endpoint if runtime_type == "openai_sidecar" else None,
            model=settings.model_name if runtime_type == "openai_sidecar" else None,
            api_key_env=settings.model_api_key_env,
            protocol=(
                "openai-compatible-sidecar"
                if runtime_type == "openai_sidecar"
                else "host-ndjson"
            ),
            provider=(
                "openai-compatible"
                if runtime_type == "openai_sidecar"
                else "fixture"
            ),
            adapter_version=f"{runtime_type}/legacy",
            adapter_digest=(
                f"sha256:{content.hexdigest()}" if paths else ""
            ),
            source="legacy_explicit_agent_mapping",
        )

    if settings.agent_builtin_enabled:
        bridge = Path(__file__).resolve().parents[3] / "bridges/pi/bridge.mjs"
        return AgentRuntimeConfig(
            type="pi", command=(shutil.which("node") or "node", str(bridge)),
            allowed_executables=("node",), endpoint=settings.agent_endpoint,
            model=settings.agent_model, api_key_env=settings.agent_api_key_env,
            protocol="host-ndjson", provider="pi", adapter_version="pi-sdk-bridge/v1",
            adapter_digest=adapter_source_digest(bridge),
            source="builtin_default",
        )

    return AgentRuntimeConfig(
        type="unavailable",
        command=(),
        allowed_executables=tuple(settings.agent_allowed_executables),
        endpoint=None,
        model=None,
        api_key_env=settings.agent_api_key_env,
        protocol="none",
        provider="unavailable",
        adapter_version="unavailable",
        adapter_digest="",
        source=(
            "auto_discovery_missing"
            if settings.agent_builtin_enabled
            else "split"
        ),
    )


def runtime_config_facts() -> dict[str, object]:
    agent = agent_runtime_config()
    return {
        "agentRuntime": {
            **asdict(agent),
            "command": list(agent.command),
            "allowed_executables": list(agent.allowed_executables),
            "configured": agent.configured,
            "digest": agent.digest,
        },
    }


def semantic_workflow_selection_available(*, probe_verified: bool = False) -> bool:
    runtime = agent_runtime_config()
    if (
        not runtime.configured
        or runtime.type not in {"traex_builtin", "traex_managed", "openai_sidecar", "pi"}
        or not runtime.command
    ):
        return False
    executable = Path(runtime.command[0])
    resolved = (
        executable
        if executable.is_absolute() and executable.is_file()
        else shutil.which(runtime.command[0])
    )
    return (
        probe_verified
        and resolved is not None
        and executable.name in runtime.allowed_executables
    )
