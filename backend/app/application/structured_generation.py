from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.compiler import digest_json
from app.application.runtime_config import (
    agent_runtime_config,
    llm_runtime_config,
)
from app.config import get_settings
from app.runtime.models import ModelRuntimeError, OpenAICompatibleAdapter

RuntimeUsed = Literal["direct_llm", "agent_fallback"]


@dataclass(frozen=True)
class StructuredGenerationRequest:
    call_id: str
    prompt: str
    output_schema: dict[str, Any]
    max_output_tokens: int
    working_directory: Path


@dataclass(frozen=True)
class StructuredGenerationRuntime:
    runtime_used: RuntimeUsed
    runtime_kind: Literal["llm_runtime", "agent_runtime"]
    provider: str
    identity: str | None
    protocol: str
    runtime_type: str
    adapter_version: str | None
    adapter_digest: str | None
    digest: str
    policy_digest: str
    fallback_reason: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtimeSelection": self.runtime_used,
            "runtimeKind": self.runtime_kind,
            "provider": self.provider,
            "identity": self.identity,
            "protocol": self.protocol,
            "runtimeType": self.runtime_type,
            "adapterVersion": self.adapter_version,
            "adapterDigest": self.adapter_digest,
            "runtimeDigest": self.digest,
            "policyDigest": self.policy_digest,
            "fallbackReason": self.fallback_reason,
        }


@dataclass(frozen=True)
class StructuredGenerationResult:
    output: dict[str, Any]
    runtime: StructuredGenerationRuntime
    input_tokens: int
    output_tokens: int
    total_tokens: int
    usage_complete: bool
    tool_request_count: int = 0
    request_dispatched: bool = True


def normalize_model_runtime_policy(
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = policy or {}
    order = raw.get("order", ["llm_runtime", "agent_runtime"])
    if order != ["llm_runtime", "agent_runtime"]:
        raise ValueError("modelRuntimePolicy.order 只支持 llm_runtime → agent_runtime")
    return {"order": order, "fallback": "startup_unavailable_only"}


def resolve_structured_generation_runtime(
    policy: dict[str, Any] | None = None,
) -> StructuredGenerationRuntime:
    normalized = normalize_model_runtime_policy(policy)
    policy_digest = digest_json(normalized)
    llm = llm_runtime_config()
    if llm.configured:
        return StructuredGenerationRuntime(
            runtime_used="direct_llm",
            runtime_kind="llm_runtime",
            provider="openai-compatible",
            identity=llm.model,
            protocol="openai-compatible-http",
            runtime_type=llm.type,
            adapter_version=None,
            adapter_digest=None,
            digest=llm.digest,
            policy_digest=policy_digest,
        )
    agent = agent_runtime_config()
    if agent.configured:
        return StructuredGenerationRuntime(
            runtime_used="agent_fallback",
            runtime_kind="agent_runtime",
            provider=agent.provider,
            identity=agent.model,
            protocol=agent.protocol,
            runtime_type=agent.type,
            adapter_version=agent.adapter_version,
            adapter_digest=agent.adapter_digest,
            digest=agent.digest,
            policy_digest=policy_digest,
            fallback_reason="LLM_RUNTIME_UNCONFIGURED",
        )
    raise ModelRuntimeError(
        "MODEL_RUNTIME_UNAVAILABLE",
        "LLM Runtime 与 Agent Runtime 均未配置",
    )


def frozen_runtime_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> StructuredGenerationRuntime:
    selection = (
        snapshot.get("runtimeSelection", snapshot.get("runtimeUsed"))
        if snapshot
        else None
    )
    if not snapshot or selection not in {
        "direct_llm",
        "agent_fallback",
    }:
        raise ModelRuntimeError(
            "MODEL_RUNTIME_UNAVAILABLE",
            "Run 未冻结可用的结构化生成 Runtime",
        )
    return StructuredGenerationRuntime(
        runtime_used=selection,
        runtime_kind=snapshot["runtimeKind"],
        provider=snapshot["provider"],
        identity=snapshot.get("identity"),
        protocol=str(snapshot.get("protocol") or "unknown"),
        runtime_type=str(snapshot.get("runtimeType") or snapshot["runtimeKind"]),
        adapter_version=snapshot.get("adapterVersion"),
        adapter_digest=snapshot.get("adapterDigest"),
        digest=snapshot["runtimeDigest"],
        policy_digest=snapshot["policyDigest"],
        fallback_reason=snapshot.get("fallbackReason"),
    )


class AgentStructuredGenerationAdapter:
    def __init__(self, host: AgentHost | None = None) -> None:
        self._host = host or AgentHost()

    async def generate(
        self,
        request: StructuredGenerationRequest,
        runtime: StructuredGenerationRuntime,
    ) -> StructuredGenerationResult:
        settings = get_settings()
        current = agent_runtime_config()
        if current.digest != runtime.digest:
            raise ModelRuntimeError(
                "MODEL_RUNTIME_CONFIG_DRIFT",
                "Agent Runtime 配置在 Run 启动后发生变化",
            )
        try:
            result = await self._host.execute(
                AgentHostRequest(
                    run_id=request.call_id,
                    prompt=(
                        "完成一次严格结构化生成。禁止调用任何工具，只返回符合 JSON Schema "
                        f"的对象。\n\n{request.prompt}"
                    ),
                    output_schema=request.output_schema,
                    working_directory=request.working_directory,
                    requested_capabilities=(),
                    max_steps=1,
                    max_total_tokens=min(
                        settings.agent_max_total_tokens,
                        max(1, request.max_output_tokens * 8),
                    ),
                    max_duration_seconds=settings.agent_max_duration_seconds,
                )
            )
        except AgentHostError as exc:
            raise ModelRuntimeError(
                exc.code,
                exc.message,
                request_dispatched=exc.request_dispatched,
            ) from exc
        tool_requests = sum(event["type"] == "tool.requested" for event in result.runtime_events)
        if tool_requests:
            raise ModelRuntimeError(
                "PERMISSION_DENIED",
                "Agent structured fallback 产生了禁止的 Tool 请求",
            )
        Draft202012Validator(request.output_schema).validate(result.output)
        return StructuredGenerationResult(
            output=result.output,
            runtime=runtime,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            usage_complete=result.usage_complete,
            tool_request_count=tool_requests,
            request_dispatched=result.request_dispatched,
        )


class StructuredGenerationResolver:
    async def generate(
        self,
        request: StructuredGenerationRequest,
        runtime_snapshot: dict[str, Any] | None,
    ) -> StructuredGenerationResult:
        runtime = frozen_runtime_from_snapshot(runtime_snapshot)
        if runtime.runtime_used == "direct_llm":
            if llm_runtime_config().digest != runtime.digest:
                raise ModelRuntimeError(
                    "MODEL_RUNTIME_CONFIG_DRIFT",
                    "LLM Runtime 配置在 Run 启动后发生变化",
                )
            result = await OpenAICompatibleAdapter().generate(
                request.prompt,
                request.output_schema,
                request.max_output_tokens,
            )
            Draft202012Validator(request.output_schema).validate(result.output)
            return StructuredGenerationResult(
                output=result.output,
                runtime=runtime,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.input_tokens + result.output_tokens,
                usage_complete=result.usage_complete,
            )
        return await AgentStructuredGenerationAdapter().generate(request, runtime)


def runtime_snapshot_for_definition(definition: Any) -> dict[str, Any]:
    policy = getattr(definition.spec, "model_runtime_policy", None)
    raw_policy = policy.model_dump(by_alias=True, mode="json") if policy is not None else None
    normalized = normalize_model_runtime_policy(raw_policy)
    try:
        runtime = resolve_structured_generation_runtime(normalized)
        return {"policy": normalized, **runtime.snapshot()}
    except ModelRuntimeError as exc:
        return {
            "policy": normalized,
            "runtimeSelection": "unavailable",
            "runtimeKind": None,
            "provider": None,
            "identity": None,
            "runtimeDigest": digest_json(
                {
                    "llm": llm_runtime_config().digest,
                    "agent": agent_runtime_config().digest,
                }
            ),
            "policyDigest": digest_json(normalized),
            "fallbackReason": exc.code,
        }
