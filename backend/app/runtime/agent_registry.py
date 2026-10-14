"""Agent transport contracts and application composition registry."""

from collections.abc import Callable
from contextlib import contextmanager

from app.application.runtime_config import agent_runtime_config
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
    @property
    def provides_output_schema(self):
        runtime = self._runtime or agent_runtime_config()
        return runtime.model_options.get("structuredMode", "json_schema") == "json_schema"

    @contextmanager
    def request_arguments(self, request: AgentRunRequest):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="workflow-pi-request-") as directory:
            path = Path(directory) / "request.json"
            path.touch(mode=0o600)
            path.write_text(json.dumps({
                "prompt": request.prompt,
                "messages": request.messages,
                "max-steps": request.max_steps,
                "max-tokens": request.max_tokens,
                "max-output-tokens": request.max_output_tokens,
                "allowed-tools": request.allowed_tools,
                "output-schema": request.output_schema,
                "tool-descriptors": request.tool_descriptors,
                "images": request.images,
            }, ensure_ascii=False), encoding="utf-8")
            yield ["--request-file", str(path)]
