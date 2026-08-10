from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config import get_settings


@dataclass(frozen=True)
class ModelResult:
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    usage_complete: bool = True


class ModelAdapter(Protocol):
    async def generate(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ModelResult: ...


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


class OpenAICompatibleAdapter:
    async def generate(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ModelResult:
        settings = get_settings()
        if not settings.model_endpoint or not settings.model_name:
            raise RuntimeError("MODEL_NOT_CONFIGURED")
        api_key = os.environ.get(settings.model_api_key_env)
        if not api_key:
            raise RuntimeError(f"缺少环境变量 {settings.model_api_key_env}")
        request = {
            "model": settings.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "workflow_node_output",
                    "strict": True,
                    "schema": output_schema,
                },
            },
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                settings.model_endpoint,
                headers={"authorization": f"Bearer {api_key}"},
                json=request,
            )
            response.raise_for_status()
            payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        output = json.loads(content) if isinstance(content, str) else content
        usage = payload.get("usage", {})
        return ModelResult(
            output=output,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=payload.get("model", settings.model_name),
            usage_complete=(
                "prompt_tokens" in usage and "completion_tokens" in usage
            ),
        )
