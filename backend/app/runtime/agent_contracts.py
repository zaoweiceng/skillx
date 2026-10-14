"""Provider-independent Agent transport contract."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentRunRequest:
    prompt: str
    working_directory: Path
    output_schema: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    tool_descriptors: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, str]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    max_steps: int = 8
    max_tokens: int = 1250000
    max_input_tokens: int = 1000000
    max_output_tokens: int = 250000
    max_billable_tokens: int = 1250000


class AgentAdapter(Protocol):
    def events(self, run_id: str, request: AgentRunRequest) -> AsyncIterator[dict[str, Any]]: ...
    async def send_tool_result(self, run_id: str, result: dict[str, Any]) -> None: ...
    async def cancel(self, run_id: str) -> None: ...
