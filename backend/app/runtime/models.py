from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from jsonschema import Draft202012Validator

from app.infrastructure.runtime_credentials import resolve_runtime_key

from app.application.runtime_config import LLMRuntimeConfig, llm_runtime_config


@dataclass(frozen=True)
class ModelResult:
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    model: str
    usage_complete: bool = True


@dataclass(frozen=True)
class ModelTextEvent:
    delta: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


def completion_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"


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


class OpenAICompatibleAdapter:
    def __init__(
        self,
        runtime: LLMRuntimeConfig | None = None,
        *,
        structured_mode: Literal["json_schema", "json_object", "prompt"] = "json_schema",
        timeout_seconds: float = 60,
    ) -> None:
        self.runtime = runtime
        self.structured_mode = structured_mode
        self.timeout_seconds = timeout_seconds

    async def stream_text(
        self, messages: list[dict[str, str]], max_output_tokens: int = 1000
    ) -> AsyncIterator[ModelTextEvent]:
        """Stream text without an Agent process; cancellation closes the HTTP stream."""
        runtime = self.runtime or llm_runtime_config()
        if not runtime.configured or not runtime.endpoint:
            raise ModelRuntimeError("MODEL_RUNTIME_UNAVAILABLE", "Direct LLM Runtime 未配置")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    completion_endpoint(runtime.endpoint),
                    headers={"authorization": f"Bearer {resolve_runtime_key(runtime.api_key_env)}"},
                    json={
                        "model": runtime.model,
                        "messages": messages,
                        "max_tokens": max_output_tokens,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                ) as response:
                    if response.is_error:
                        code = (
                            "MODEL_AUTH_FAILED"
                            if response.status_code in {401, 403}
                            else "RATE_LIMITED"
                            if response.status_code == 429
                            else "MODEL_PROVIDER_ERROR"
                        )
                        raise ModelRuntimeError(
                            code, f"HTTP {response.status_code}", request_dispatched=True
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        event = json.loads(payload)
                        if event.get("error"):
                            raise ModelRuntimeError(
                                "MODEL_PROVIDER_ERROR", "模型流返回错误", request_dispatched=True
                            )
                        for choice in event.get("choices", []):
                            delta = choice.get("delta", {}).get("content")
                            if isinstance(delta, str) and delta:
                                yield ModelTextEvent(delta=delta)
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            yield ModelTextEvent(
                                input_tokens=usage.get("prompt_tokens"),
                                output_tokens=usage.get("completion_tokens"),
                            )
                    raise ModelRuntimeError(
                        "MODEL_MALFORMED_OUTPUT", "模型流未完整结束", request_dispatched=True
                    )
        except httpx.TimeoutException as exc:
            raise ModelRuntimeError("TIMEOUT", "模型流超时", request_dispatched=True) from exc
        except httpx.HTTPError as exc:
            raise ModelRuntimeError(
                "DEPENDENCY_UNAVAILABLE", "模型连接失败", request_dispatched=True
            ) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise ModelRuntimeError(
                "MODEL_MALFORMED_OUTPUT", "模型流格式错误", request_dispatched=True
            ) from exc

    async def generate(
        self,
        prompt: str,
        output_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> ModelResult:
        runtime = self.runtime or llm_runtime_config()
        if not runtime.configured or not runtime.endpoint or not runtime.model:
            raise ModelRuntimeError("MODEL_RUNTIME_UNAVAILABLE", "Direct LLM Runtime 未配置")
        api_key = resolve_runtime_key(runtime.api_key_env)
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
        if self.structured_mode != "json_schema":
            request.pop("response_format")
            request["messages"][0]["content"] += (
                "\nReturn only a JSON object matching this schema: " + json.dumps(output_schema)
            )
            if self.structured_mode == "json_object":
                request["response_format"] = {"type": "json_object"}
        request_dispatched = False
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                request_dispatched = True
                response = await client.post(
                    (
                        runtime.endpoint.rstrip("/")
                        if runtime.endpoint.rstrip("/").endswith("/chat/completions")
                        else runtime.endpoint.rstrip("/") + "/chat/completions"
                    ),
                    headers={"authorization": f"Bearer {api_key}"},
                    json=request,
                )
            if response.status_code in {401, 403}:
                raise ModelRuntimeError(
                    "MODEL_AUTH_FAILED",
                    "LLM Provider 鉴权失败",
                    request_dispatched=True,
                )
            if response.status_code == 429:
                raise ModelRuntimeError(
                    "RATE_LIMITED",
                    "LLM Provider 请求限流",
                    request_dispatched=True,
                )
            if response.is_error:
                raise ModelRuntimeError(
                    "MODEL_PROVIDER_ERROR",
                    f"LLM Provider 返回 HTTP {response.status_code}",
                    request_dispatched=True,
                )
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            output = json.loads(content) if isinstance(content, str) else content
        except ModelRuntimeError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelRuntimeError(
                "TIMEOUT",
                "LLM Provider 请求超时",
                request_dispatched=request_dispatched,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelRuntimeError(
                "DEPENDENCY_UNAVAILABLE",
                "LLM Provider 连接失败",
                request_dispatched=request_dispatched,
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelRuntimeError(
                "MODEL_MALFORMED_OUTPUT",
                "LLM Provider 返回无法解析的结构化结果",
                request_dispatched=request_dispatched,
            ) from exc
        if not isinstance(output, dict) or list(
            Draft202012Validator(output_schema).iter_errors(output)
        ):
            raise ModelRuntimeError(
                "MODEL_MALFORMED_OUTPUT", "模型结果不满足输出契约", request_dispatched=True
            )
        usage = payload.get("usage") or {}
        return ModelResult(
            output=output,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=payload.get("model", runtime.model),
            usage_complete=("prompt_tokens" in usage and "completion_tokens" in usage),
        )
