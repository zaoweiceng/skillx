from dataclasses import dataclass, field
from typing import Any
from typing import Literal

from app.domain.serialization import digest_json


def _has_key(reference: str) -> bool:
    from app.infrastructure.runtime_credentials import resolve_runtime_key

    return bool(resolve_runtime_key(reference))


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
            and _has_key(self.api_key_env)
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
    type: Literal[
        "pi",
        "traex_builtin",
        "traex_managed",
        "openai_sidecar",
        "fixture",
        "unavailable",
    ]
    command: tuple[str, ...]
    allowed_executables: tuple[str, ...]
    endpoint: str | None
    model: str | None
    api_key_env: str
    protocol: str
    provider: str
    adapter_version: str
    adapter_digest: str
    source: str = "split"
    reasoning_effort: str | None = None
    model_options: dict[str, Any] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        if self.type == "unavailable" or not self.command:
            return False
        if self.type in {"openai_sidecar", "pi"}:
            return bool(self.endpoint and self.model and _has_key(self.api_key_env))
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
                "protocol": self.protocol,
                "provider": self.provider,
                "adapterVersion": self.adapter_version,
                "adapterDigest": self.adapter_digest,
                "reasoningEffort": self.reasoning_effort,
                "modelOptions": self.model_options,
            }
        )

