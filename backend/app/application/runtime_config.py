from __future__ import annotations

import logging
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from app.application.compiler import digest_json
from app.config import get_settings

logger = logging.getLogger(__name__)
_legacy_logged = False


@dataclass(frozen=True)
class LLMRuntimeConfig:
    type: Literal["openai_compatible", "unavailable"]
    endpoint: str | None
    model: str | None
    api_key_env: str
    source: str = "split"

    @property
    def configured(self) -> bool:
        return bool(
            self.type == "openai_compatible"
            and self.endpoint
            and self.model
            and os.environ.get(self.api_key_env)
        )

    @property
    def digest(self) -> str:
        return digest_json(
            {
                "type": self.type,
                "endpoint": self.endpoint,
                "model": self.model,
                "apiKeyEnv": self.api_key_env,
            }
        )


@dataclass(frozen=True)
class AgentRuntimeConfig:
    type: Literal["traex_managed", "openai_sidecar", "fixture", "unavailable"]
    command: tuple[str, ...]
    allowed_executables: tuple[str, ...]
    endpoint: str | None
    model: str | None
    api_key_env: str
    source: str = "split"

    @property
    def configured(self) -> bool:
        if self.type == "unavailable" or not self.command:
            return False
        if self.type == "openai_sidecar":
            return bool(self.endpoint and self.model and os.environ.get(self.api_key_env))
        return True

    @property
    def digest(self) -> str:
        return digest_json(
            {
                "type": self.type,
                "command": list(self.command),
                "allowedExecutables": list(self.allowed_executables),
                "endpoint": self.endpoint,
                "model": self.model,
                "apiKeyEnv": self.api_key_env,
            }
        )


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
    settings = get_settings()
    if settings.agent_runtime_type != "unavailable" or settings.agent_command is not None:
        return AgentRuntimeConfig(
            type=settings.agent_runtime_type,
            command=tuple(settings.agent_command or ()),
            allowed_executables=tuple(settings.agent_allowed_executables),
            endpoint=settings.agent_endpoint,
            model=settings.agent_model,
            api_key_env=settings.agent_api_key_env,
        )

    if settings.agent_provider_type != "unavailable" or settings.traex_command:
        _log_legacy_mapping()
        runtime_type: Literal["traex_managed", "openai_sidecar", "fixture", "unavailable"] = {
            "real": "openai_sidecar",
            "fixture": "fixture",
            "unavailable": "unavailable",
        }[settings.agent_provider_type]
        return AgentRuntimeConfig(
            type=runtime_type,
            command=tuple(settings.traex_command or ()),
            allowed_executables=tuple(settings.traex_allowed_executables),
            endpoint=settings.model_endpoint if runtime_type == "openai_sidecar" else None,
            model=settings.model_name if runtime_type == "openai_sidecar" else None,
            api_key_env=settings.model_api_key_env,
            source="legacy_explicit_agent_mapping",
        )

    return AgentRuntimeConfig(
        type="unavailable",
        command=(),
        allowed_executables=tuple(settings.agent_allowed_executables),
        endpoint=None,
        model=None,
        api_key_env=settings.agent_api_key_env,
    )


def runtime_config_facts() -> dict[str, object]:
    llm = llm_runtime_config()
    agent = agent_runtime_config()
    return {
        "llmRuntime": {
            **asdict(llm),
            "configured": llm.configured,
            "digest": llm.digest,
        },
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
        or runtime.type not in {"traex_managed", "openai_sidecar"}
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
