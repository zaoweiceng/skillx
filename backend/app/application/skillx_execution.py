"""The same pinned SkillX invocation implementation for manual and DAG callers."""

from __future__ import annotations

import asyncio
import json

from jsonschema import Draft202012Validator

from app.application.agent_host import AgentHost, AgentHostRequest
from app.application.capabilities import resolve_package_path
from app.application.runtime_profiles import restore_agent_snapshot
from app.application.secrets import resolve_secret_handles, secret_names_for_interface
from app.config import get_settings
from app.domain.workflow import ExecutionError, NodeResult
from app.infrastructure.database import CapabilityVersion, SessionLocal
from app.runtime.skillx import package_digest
from app.runtime.skillx_host import execute_in_subprocess


async def execute_skillx(
    version_id: str,
    interface_name: str,
    inputs: dict,
    invocation_id: str,
    runtime: dict | None = None,
    cancelled=None,
    effect_approved: bool = False,
    depth: int = 0,
    *,
    _candidate_version=None,
) -> NodeResult:
    if depth > 8:
        raise ValueError("SkillX 依赖调用超过 8 层")
    async with SessionLocal() as session:
        version = _candidate_version or await session.get(CapabilityVersion, version_id)
        if version is None or version.status != "published":
            raise ValueError("SkillX 版本不存在")
        from app.application.capability_availability import require_enabled
        await require_enabled(session, version.capability_id)
        package = resolve_package_path(version.package_path)
        if package_digest(package) != version.digest:
            raise ValueError("SkillX 内容与锁定 digest 不一致")
        interface = version.manifest_json["interfaces"][interface_name]
        raw = version.manifest_json["manifest"]["spec"]["interfaces"][interface_name]
        locks = version.manifest_json.get("dependencyLocks", {})
        Draft202012Validator(interface["inputSchema"]).validate(inputs)
        secrets = await resolve_secret_handles(
            session, secret_names_for_interface(version.manifest_json["manifest"], interface_name)
        )
    effects = raw.get("effects", [])
    if not effect_approved and any(e.get("confirmation") == "required" for e in effects):
        # Never bypass an effect confirmation through the manual or task entrypoint.
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="APPROVAL_REQUIRED", message="此能力要求操作审批，请通过带审批节点的工作流调用"
            ),
        )
    timeout = int(raw.get("execution", {}).get("timeoutSeconds", 60))

    async def dependency(alias, arguments):
        if alias not in raw.get("capabilities", []) or alias not in locks:
            raise PermissionError("依赖不在接口声明中")
        lock = locks[alias]
        if lock["kind"] == "skillx":
            async with SessionLocal() as session:
                target = await session.get(CapabilityVersion, lock["versionId"])
                if not target or target.digest != lock["digest"]:
                    raise ValueError("依赖锁漂移")
            result = await execute_skillx(
                lock["versionId"],
                lock["interface"],
                arguments,
                invocation_id + "-" + alias,
                runtime,
                cancelled,
                effect_approved=effect_approved,
                depth=depth + 1,
            )
            if result.status != "succeeded":
                raise ValueError(result.error.message if result.error else "依赖失败")
            return result.output
        if lock["kind"] == "mcp":
            from app.application.mcp import call_snapshot_through_contract
            from app.infrastructure.database import MCPToolSnapshot

            async with SessionLocal() as session:
                snapshot = await session.get(MCPToolSnapshot, lock["snapshotId"])
                if not snapshot or snapshot.digest != lock["digest"]:
                    raise ValueError("MCP 依赖漂移")
                if (
                    snapshot.side_effect not in {"none", "read", "data.read"}
                    and not effect_approved
                ):
                    raise PermissionError("MCP 写入需要本次授权")
                outcome = await call_snapshot_through_contract(session, snapshot.id, arguments)
            if outcome.status != "succeeded":
                raise ValueError(outcome.error_message)
            return outcome.output
        from app.application.tool_broker import ToolAdapterRegistry, ToolExecutionContext

        registry = ToolAdapterRegistry()
        if registry.describe(lock["capabilityRef"])["capabilityIdentity"] != lock["digest"]:
            raise ValueError("工具依赖漂移")
        if lock["capabilityRef"] not in get_settings().agent_host_capabilities:
            raise PermissionError("工具依赖未授权")
        return await registry.execute(
            lock["capabilityRef"],
            ToolExecutionContext(
                invocation_id,
                invocation_id + "-" + alias,
                get_settings().artifact_root / "skillx" / invocation_id,
            ),
            arguments,
        )

    async def perform():
        if raw["kind"] == "direct":
            working_directory = None
            if raw.get("x-executionProfile") == "local-command":
                from app.application.local_execution import validate_working_directory
                if not get_settings().task_local_execution_enabled:
                    raise PermissionError("本机命令执行尚未启用")
                directory = (runtime or {}).get("workingDirectory")
                if directory:
                    working_directory = validate_working_directory(directory)
            return await execute_in_subprocess(
                package, interface_name, invocation_id, inputs, secrets, dependency,
                working_directory=working_directory,
            )
        entry = raw.get("prompt", {}).get("entry")
        prompt_path = (package / str(entry)).resolve()
        if (
            not entry
            or not prompt_path.is_relative_to(package.resolve())
            or not prompt_path.is_file()
        ):
            raise ValueError("SkillX 模型接口缺少有效 prompt.entry")
        prompt = prompt_path.read_text() + "\nINPUT=" + json.dumps(inputs, ensure_ascii=False)
        if raw["kind"] == "llm":
            from app.application.structured_generation import (
                StructuredGenerationRequest,
                StructuredGenerationResolver,
            )

            result = await StructuredGenerationResolver().generate(
                StructuredGenerationRequest(
                    call_id=invocation_id,
                    prompt=prompt,
                    output_schema=interface["outputSchema"],
                    max_output_tokens=raw.get("model", {}).get("maxOutputTokens", 4096),
                    working_directory=get_settings().artifact_root / "skillx" / invocation_id,
                ),
                (runtime or {}).get("modelSnapshot"),
            )
        else:
            from app.application.tool_broker import ToolAdapterRegistry

            class LockedDependency:
                input_schema = {"type": "object"}
                output_schema = {"type": "object"}
                effect = "none"
                idempotent = False
                fixture_policy = "required"

                def __init__(self, alias):
                    self.capability_ref = alias
                    self.effect = locks[alias].get("effect", "unknown")

                async def execute(self, context, arguments):
                    return await dependency(self.capability_ref, arguments)

            limits = raw.get("agent", {})
            aliases = frozenset(limits.get("allowedCapabilities", raw.get("capabilities", [])))
            if not aliases.issubset(raw.get("capabilities", [])):
                raise PermissionError("Agent 能力不在 SkillX 接口依赖声明中")
            result = await AgentHost(
                runtime=restore_agent_snapshot((runtime or {}).get("agentConfig", runtime)),
                tool_registry=ToolAdapterRegistry([LockedDependency(alias) for alias in aliases]),
                capability_grants=aliases,
            ).execute(
                AgentHostRequest(
                    run_id=invocation_id,
                    include_managed_skills=False,
                    prompt=prompt,
                    output_schema=interface["outputSchema"],
                    working_directory=get_settings().artifact_root / "skillx" / invocation_id,
                    requested_capabilities=tuple(aliases),
                    max_steps=limits.get("maxSteps", 6),
                    max_total_tokens=limits.get(
                        "maxTotalTokens", get_settings().agent_max_total_tokens
                    ),
                    max_duration_seconds=timeout,
                ),
                cancellation=cancelled,
            )
        return NodeResult(
            status="succeeded", output=result.output, metrics={"totalTokens": result.total_tokens}
        )

    operation = asyncio.create_task(perform())
    try:
        async with asyncio.timeout(timeout):
            while not operation.done():
                if cancelled and await cancelled():
                    raise asyncio.CancelledError()
                await asyncio.wait({operation}, timeout=0.2)
            return await operation
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
