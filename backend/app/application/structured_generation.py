from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator

from app.application.compiler import digest_json
from app.application.runtime_config import agent_runtime_config
from app.runtime.models import ModelRuntimeError

RuntimeUsed = Literal["agent_structured"]


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
    runtime_kind: Literal["agent_runtime"]
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
    # Legacy policy fields remain readable, but cannot enable a second runtime.
    return {"order": ["agent_runtime"]}


def resolve_structured_generation_runtime(policy=None) -> StructuredGenerationRuntime:
    agent = agent_runtime_config()
    if not agent.configured:
        raise ModelRuntimeError("MODEL_RUNTIME_UNAVAILABLE", "请配置 Pi 或其他 Agent")
    return frozen_runtime_from_snapshot(agent_structured_snapshot(asdict(agent)))


def agent_structured_snapshot(agent):
    return {
        "runtimeSelection": "agent_structured", "agentConfig": agent,
        "runtimeKind": "agent_runtime", "provider": agent.get("provider", "agent"),
        "identity": agent.get("model"), "protocol": "agent-host",
        "runtimeType": agent["type"], "runtimeDigest": digest_json(agent),
        "policyDigest": digest_json({"order": ["agent_runtime"], "tools": False}),
    }


def frozen_runtime_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> StructuredGenerationRuntime:
    selection = snapshot.get("runtimeSelection", snapshot.get("runtimeUsed")) if snapshot else None
    if not snapshot or selection not in {
        "agent_structured",
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
    )


class StructuredGenerationResolver:
    async def generate(
        self,
        request: StructuredGenerationRequest,
        runtime_snapshot: dict[str, Any] | None,
    ) -> StructuredGenerationResult:
        runtime = frozen_runtime_from_snapshot(runtime_snapshot)
        if runtime.runtime_used == "agent_structured":
            from app.application.agent_host import AgentHost, AgentHostRequest, AgentHostError
            from app.application.runtime_profiles import restore_agent_snapshot
            agent = (runtime_snapshot or {}).get("agentConfig")
            if not agent or digest_json(agent) != runtime.digest:
                raise ModelRuntimeError("MODEL_RUNTIME_CONFIG_DRIFT", "Agent 结构化生成配置已变化")
            selected = restore_agent_snapshot(agent)
            if not selected.configured:
                raise ModelRuntimeError("MODEL_RUNTIME_UNAVAILABLE", "请配置 Pi 或其他 Agent")
            try:
                result = await AgentHost(runtime=selected).execute(AgentHostRequest(
                run_id=request.call_id, prompt=request.prompt, output_schema=request.output_schema,
                working_directory=request.working_directory, max_steps=1,
                max_output_tokens=request.max_output_tokens, include_managed_skills=False,
                capability_catalog_included=True, requested_capabilities=(),
                ))
            except AgentHostError as exc:
                raise ModelRuntimeError(exc.code, str(exc), request_dispatched=exc.request_dispatched) from exc
            Draft202012Validator(request.output_schema).validate(result.output)
            return StructuredGenerationResult(output=result.output, runtime=runtime,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                total_tokens=result.total_tokens, usage_complete=result.usage_complete)


def runtime_snapshot_for_definition(definition: Any) -> dict[str, Any]:
    return {"policy": {"order": ["agent_runtime"]},
            **agent_structured_snapshot(asdict(agent_runtime_config()))}
