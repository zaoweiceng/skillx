#!/usr/bin/env python3
"""OpenAI-compatible Agent sidecar for the SkillX Host event protocol."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "lookup.table": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
    "workflow.search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "constraints": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "workflow.describe": {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
        "additionalProperties": False,
    },
    "workflow.describe_fragment": {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
        "additionalProperties": False,
    },
    "workflow.slice": {
        "type": "object",
        "properties": {
            "workflowRef": {"type": "string"},
            "intent": {"type": "string"},
            "candidateNodeIds": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["workflowRef", "intent"],
        "additionalProperties": False,
    },
    "workflow.compile_plan": {
        "type": "object",
        "properties": {"plan": {"type": "object"}},
        "required": ["plan"],
        "additionalProperties": False,
    },
}


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def fail(code: str, message: str, *, retryable: bool = False) -> int:
    emit(
        {
            "type": "agent.failed",
            "payload": {"code": code, "message": message, "retryable": retryable},
        }
    )
    return 0


def provider_config() -> tuple[str, str, str]:
    endpoint = os.environ.get("SKILLX_MODEL_ENDPOINT", "")
    model = os.environ.get("SKILLX_MODEL_NAME", "")
    key_env = os.environ.get("SKILLX_MODEL_API_KEY_ENV", "SKILLX_MODEL_API_KEY")
    api_key = os.environ.get(key_env, "")
    if not endpoint or not model or not api_key:
        raise RuntimeError(
            "缺少 SKILLX_MODEL_ENDPOINT、SKILLX_MODEL_NAME 或模型 API Key"
        )
    return endpoint, model, api_key


def post_completion(payload: dict[str, Any]) -> dict[str, Any]:
    endpoint, _model, api_key = provider_config()
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            decoded = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        lowered = detail.lower()
        if exc.code in {401, 403}:
            code, retryable = "MODEL_AUTH_FAILED", False
        elif exc.code == 429:
            code, retryable = "RATE_LIMITED", True
        elif exc.code in {400, 404, 422} and any(
            marker in lowered
            for marker in (
                "json_schema",
                "response_format",
                "tool_choice",
                "tool_calls",
                "function calling",
            )
        ):
            code, retryable = "MODEL_CAPABILITY_UNSUPPORTED", False
        elif exc.code >= 500:
            code, retryable = "DEPENDENCY_UNAVAILABLE", True
        else:
            code, retryable = "MODEL_PROVIDER_ERROR", False
        raise ProviderError(
            code,
            f"模型 HTTP {exc.code}: {detail}",
            retryable=retryable,
        ) from exc
    except TimeoutError as exc:
        raise ProviderError("TIMEOUT", "模型请求超时", retryable=True) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(
            "DEPENDENCY_UNAVAILABLE",
            f"模型网络请求失败: {exc.reason}",
            retryable=True,
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "MODEL_MALFORMED_OUTPUT",
            "模型 HTTP 响应不是合法 JSON",
            retryable=False,
        ) from exc
    if not isinstance(decoded, dict) or not decoded.get("choices"):
        raise ProviderError(
            "MODEL_MALFORMED_OUTPUT",
            "模型响应缺少 choices",
            retryable=False,
        )
    return decoded


def parse_output(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        output = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "MODEL_MALFORMED_OUTPUT",
            "模型最终输出不是合法 JSON",
            retryable=False,
        ) from exc
    if not isinstance(output, dict):
        raise ProviderError(
            "MODEL_MALFORMED_OUTPUT",
            "模型最终输出必须为 JSON 对象",
            retryable=False,
        )
    return output


def read_tool_result(call_id: str) -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("Host 未回送 ToolResult")
    event = json.loads(line)
    payload = event.get("payload", {})
    if event.get("type") != "tool.result" or payload.get("callId") != call_id:
        raise RuntimeError("Host ToolResult 协议不匹配")
    return payload


def tool_name(capability: str) -> str:
    return capability.replace(".", "__")


def tool_descriptors(allowed: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    reverse: dict[str, str] = {}
    descriptors = []
    for capability in allowed:
        name = tool_name(capability)
        reverse[name] = capability
        descriptors.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        f"请求 Host 执行受控能力 {capability}。"
                        "不得假设结果，必须等待 Host ToolResult。"
                    ),
                    "parameters": TOOL_SCHEMAS.get(
                        capability,
                        {"type": "object", "additionalProperties": True},
                    ),
                },
            }
        )
    return descriptors, reverse


def run(args: argparse.Namespace) -> int:
    _endpoint, model, _api_key = provider_config()
    allowed = json.loads(args.allowed_tools)
    output_schema = json.loads(args.output_schema)
    tools, reverse_names = tool_descriptors(allowed)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是 SkillX 的受限执行 Agent。只可调用给定工具；工具由 Host 执行。"
                "最终必须返回严格符合给定 JSON Schema 的对象，不输出 Markdown。"
                f"\nJSON Schema={json.dumps(output_schema, ensure_ascii=False)}"
            ),
        },
        {"role": "user", "content": args.prompt},
    ]
    emit({"type": "agent.plan", "payload": {"summary": "由真实模型规划并受 Host 约束执行"}})
    total_tokens = 0
    usage_complete = True
    tool_results: list[dict[str, Any]] = []
    for step in range(1, args.max_steps + 1):
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, min(args.max_tokens - total_tokens, 8000)),
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        if output_schema:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_output",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        response = post_completion(request)
        usage = response.get("usage") or {}
        usage_complete = usage_complete and {
            "prompt_tokens",
            "completion_tokens",
        }.issubset(usage)
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens += input_tokens + output_tokens
        emit(
            {
                "type": "agent.step.completed",
                "payload": {
                    "step": step,
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                },
            }
        )
        if total_tokens > args.max_tokens:
            return fail("TOKEN_BUDGET_EXCEEDED", "模型 Token 超过 sidecar 上限")
        message = response["choices"][0].get("message") or {}
        calls = message.get("tool_calls") or []
        if calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": calls,
                }
            )
            for call in calls:
                function = call.get("function") or {}
                capability = reverse_names.get(str(function.get("name")))
                if capability is None:
                    return fail(
                        "PERMISSION_DENIED",
                        f"模型请求了未授权 Tool: {function.get('name')}",
                    )
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    return fail("INVALID_TOOL_ARGUMENTS", "模型 Tool 参数不是合法 JSON")
                call_id = str(call.get("id") or f"call-{step}")
                emit(
                    {
                        "type": "tool.requested",
                        "payload": {
                            "callId": call_id,
                            "capabilityRef": capability,
                            "arguments": arguments,
                        },
                    }
                )
                result = read_tool_result(call_id)
                tool_results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(
                            result.get("output")
                            if result.get("status") == "succeeded"
                            else {"error": result.get("error")},
                            ensure_ascii=False,
                        ),
                    }
                )
            continue
        output = parse_output(message.get("content"))
        provenance = {
            field: {
                "mode": "generative",
                "sourceCallIds": [item["callId"] for item in tool_results],
                "evidenceDigests": [
                    item["evidenceDigest"] for item in tool_results
                ],
                "promptVersion": "openai-agent-sidecar-v1",
            }
            for field in output
        }
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": step,
                    "totalTokens": total_tokens,
                    "usageComplete": usage_complete,
                    "output": output,
                    "outputProvenance": provenance,
                },
            }
        )
        return 0
    return fail("STEP_BUDGET_EXCEEDED", "模型未在最大步数内完成任务")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default="")
    parser.add_argument("--max-steps", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=30000)
    parser.add_argument("--allowed-tools", default="[]")
    parser.add_argument("--output-schema", default="{}")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    try:
        if args.probe:
            _endpoint, model, _api_key = provider_config()
            response = post_completion(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with JSON: {\"ok\":true}"}],
                    "max_tokens": 16,
                    "response_format": {"type": "json_object"},
                }
            )
            print(json.dumps({"ok": bool(response.get("choices")), "model": model}))
            return 0
        return run(args)
    except ProviderError as exc:
        if args.probe:
            print(json.dumps({"ok": False, "code": exc.code, "error": str(exc)}))
            return 1
        return fail(exc.code, str(exc), retryable=exc.retryable)
    except Exception as exc:  # noqa: BLE001 - process boundary reports stable failure.
        if args.probe:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        return fail("MODEL_PROVIDER_ERROR", str(exc), retryable=True)


if __name__ == "__main__":
    raise SystemExit(main())
