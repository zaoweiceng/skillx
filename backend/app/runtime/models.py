from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.application.runtime_config import llm_runtime_config


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


class ModelRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class OpenAICompatibleAdapter:
    async def generate(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ModelResult:
        runtime = llm_runtime_config()
        if not runtime.configured or not runtime.endpoint or not runtime.model:
            raise ModelRuntimeError(
                "MODEL_RUNTIME_UNAVAILABLE", "Direct LLM Runtime 未配置"
            )
        api_key = os.environ.get(runtime.api_key_env)
        if not api_key:
            raise ModelRuntimeError(
                "MODEL_RUNTIME_UNAVAILABLE",
                f"缺少环境变量 {runtime.api_key_env}",
            )
        request = {
            "model": runtime.model,
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
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    runtime.endpoint,
                    headers={"authorization": f"Bearer {api_key}"},
                    json=request,
                )
            if response.status_code in {401, 403}:
                raise ModelRuntimeError("MODEL_AUTH_FAILED", "LLM Provider 鉴权失败")
            if response.status_code == 429:
                raise ModelRuntimeError("RATE_LIMITED", "LLM Provider 请求限流")
            if response.is_error:
                raise ModelRuntimeError(
                    "MODEL_PROVIDER_ERROR",
                    f"LLM Provider 返回 HTTP {response.status_code}",
                )
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            output = json.loads(content) if isinstance(content, str) else content
        except ModelRuntimeError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelRuntimeError("TIMEOUT", "LLM Provider 请求超时") from exc
        except httpx.HTTPError as exc:
            raise ModelRuntimeError(
                "DEPENDENCY_UNAVAILABLE", "LLM Provider 连接失败"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelRuntimeError(
                "MODEL_MALFORMED_OUTPUT", "LLM Provider 返回无法解析的结构化结果"
            ) from exc
        usage = payload.get("usage", {})
        return ModelResult(
            output=output,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=payload.get("model", runtime.model),
            usage_complete=(
                "prompt_tokens" in usage and "completion_tokens" in usage
            ),
        )
