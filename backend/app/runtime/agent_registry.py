"""Agent transport contracts and application composition registry."""

from collections.abc import Callable

from app.domain.runtime import AgentRuntimeConfig
from app.runtime.adapters import AdapterError, AgentRunRequest, TraeXSubprocessAdapter
from app.runtime.agent_contracts import AgentAdapter as AgentAdapter
from app.runtime.traex_builtin import TraeXBuiltinAdapter

AdapterFactory = Callable[[AgentRuntimeConfig], AgentAdapter]


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, kind: str, factory: AdapterFactory) -> None:
        if kind in self._factories:
            raise ValueError(f"Agent adapter already registered: {kind}")
        self._factories[kind] = factory

    def create(self, runtime: AgentRuntimeConfig) -> AgentAdapter:
        factory = self._factories.get(runtime.type)
        if factory is None:
            raise AdapterError(f"Unsupported Agent adapter: {runtime.type}")
        return factory(runtime)


def default_agent_registry() -> AgentAdapterRegistry:
    registry = AgentAdapterRegistry()
    registry.register(
        "traex_builtin",
        lambda runtime: TraeXBuiltinAdapter(
            list(runtime.command), model=runtime.model, reasoning_effort=runtime.reasoning_effort
        ),
    )
    for kind in ("traex_managed", "openai_sidecar", "fixture"):
        registry.register(
            kind,
            lambda runtime: TraeXSubprocessAdapter(
                list(runtime.command), set(runtime.allowed_executables), runtime
            ),
        )
    registry.register(
        "pi",
        lambda runtime: PiAdapter(list(runtime.command), set(runtime.allowed_executables), runtime),
    )
    return registry


class PiAdapter(TraeXSubprocessAdapter):
    """Pi SDK bridge; all custom tools are executed by the Host broker."""

    supports_tool_descriptors = True

    def extra_arguments(self, request: AgentRunRequest) -> list[str]:
        import json

        return ["--tool-descriptors", json.dumps(request.tool_descriptors)]
