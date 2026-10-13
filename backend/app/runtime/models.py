from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any




@dataclass(frozen=True)
class ModelResult:
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    usage_complete: bool = True


class FakeModelAdapter:
    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output

    async def generate(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ModelResult:
        return ModelResult(
            output=self.output,
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(json.dumps(self.output, ensure_ascii=False)) // 4),
            model="fake-structured",
        )


class ModelRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_dispatched: bool = False,
    ) -> None:
        self.code = code
        self.message = message
        self.request_dispatched = request_dispatched
        super().__init__(message)
