from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.artifact_integrity import (
    artifact_file_matches,
    safe_artifact_path,
)
from app.application.compiler import digest_json
from app.application.mcp import call_mcp_contract
from app.application.runtime_config import agent_runtime_config
from app.application.secrets import contains_secret
from app.application.security import redact
from app.application.structured_generation import (
    StructuredGenerationRequest,
    StructuredGenerationResolver,
)
from app.config import get_settings
from app.domain.workflow import ExecutionError, NodeResult, NodeType, WorkflowNode
from app.runtime.browser import (
    BrowserReadContract,
    ObservedPage,
    PageContract,
    authorization_digest,
    detect_drift,
    drift_authorization_digest,
    url_matches,
)
from app.runtime.contract_patch import ContractPatchViolation, build_contract_patch
from app.runtime.execution_contracts import (
    CancellationToken as CancellationToken,
)
from app.runtime.execution_contracts import (
    ExecutionCancelledError as ExecutionCancelledError,
)
from app.runtime.execution_contracts import (
    NodeExecutor as NodeExecutor,
)
from app.runtime.execution_contracts import (
    RunContext as RunContext,
)
from app.runtime.models import FakeModelAdapter, ModelRuntimeError
from app.runtime.playwright_browser import (
    BrowserExecutionError,
    BrowserSubmitUnknownOutcome,
    BrowserTransportError,
    PlaywrightPageSession,
)
from app.runtime.skillx_host import execute_in_subprocess


def _staging_dir(run_id: str, node_run_id: str) -> Path:
    """节点执行期间的暂存目录，按 node_run 隔离。

    Executor 先把 Artifact 写到这里；只有 NodeRun 结果 CAS 成功后，编排器才把文件
    原子落位到最终路径并登记数据库记录。CAS 失败、取消或超时时暂存目录整体清理，
    不会留下孤儿文件。
    """

    return (get_settings().artifact_root / run_id / ".staging" / node_run_id).resolve()


def clear_staging(run_id: str, node_run_id: str) -> None:
    shutil.rmtree(_staging_dir(run_id, node_run_id), ignore_errors=True)


def list_final_files(run_id: str) -> set[str]:
    """列出某 run 最终目录下已落位的文件名（不含 .staging）。"""

    run_root = (get_settings().artifact_root / run_id).resolve()
    if not run_root.exists():
        return set()
    return {
        entry.name for entry in run_root.iterdir() if entry.is_file() and entry.name != ".staging"
    }


def promote_staged_artifact(run_id: str, artifact: dict[str, Any]) -> None:
    """把暂存文件原子移动到 run 根下的最终 storageKey 位置。

    幂等成功必须以 size/sha256 一致为前提；损坏 target 只能由有效 staging 修复。
    """

    staging_key = artifact.get("stagingKey")
    if not staging_key:
        return
    run_root = (get_settings().artifact_root / run_id).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    staging_root = (run_root / ".staging").resolve()
    source = safe_artifact_path(
        staging_root,
        f"{staging_key['nodeRunId']}/{staging_key['file']}",
    )
    target = safe_artifact_path(
        run_root,
        artifact["storageKey"],
        forbidden_root=staging_root,
    )
    expected = {
        "size": int(artifact["size"]),
        "sha256": str(artifact["sha256"]),
    }
    if target.exists():
        if artifact_file_matches(target, **expected):
            source.unlink(missing_ok=True)
            return
        if not source.exists() or not artifact_file_matches(source, **expected):
            raise ValueError("Artifact target 已损坏且无有效 staging 可恢复")
        target.unlink()
    if not source.exists():
        return
    if not artifact_file_matches(source, **expected):
        raise ValueError("Artifact staging 内容与登记的 size/sha256 不一致")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


def write_json_artifact(
    run_id: str,
    node_run_id: str,
    node_id: str,
    name: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = str(uuid4())
    staging = _staging_dir(run_id, node_run_id)
    staging.mkdir(parents=True, exist_ok=True)
    target = (staging / artifact_id).resolve()
    content = json.dumps(redact(payload), ensure_ascii=False, indent=2).encode()
    target.write_bytes(content)
    return {
        "id": artifact_id,
        "kind": kind,
        "name": name,
        "uri": f"artifact://{artifact_id}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mediaType": "application/json",
        "storageKey": artifact_id,
        "stagingKey": {"nodeRunId": node_run_id, "file": artifact_id},
        "metadata": {"nodeId": node_id},
    }


class InputExecutor:
    node_type = NodeType.INPUT

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        context.cancellation.check()
        return NodeResult(status="succeeded", output=context.run_input)


class OutputExecutor:
    node_type = NodeType.OUTPUT

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        return NodeResult(status="succeeded", output=inputs)


class TransformExecutor:
    node_type = NodeType.TRANSFORM

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        operation = node.config.get("operation", "passthrough")
        # 确定性可重试失败注入（仅用于恢复演示/测试）：返回 retryable DEPENDENCY_UNAVAILABLE，
        # 供 fallback checkpoint 接力验收。真实生产工作流不应使用该 operation。
        if operation == "fail_dependency":
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="DEPENDENCY_UNAVAILABLE",
                    message="注入的可重试依赖故障",
                    retryable=True,
                ),
            )
        try:
            if operation == "passthrough":
                output = inputs
            elif operation == "pick":
                fields = node.config.get("fields", [])
                output = {key: inputs[key] for key in fields if key in inputs}
            elif operation == "rename":
                mapping = node.config.get("mapping", {})
                output = {mapping.get(key, key): value for key, value in inputs.items()}
            elif operation == "template":
                template = node.config.get("template", "")
                output = {"text": template.format_map(_SafeFormat(inputs))}
            elif operation == "json":
                output = json.loads(inputs["text"])
                if not isinstance(output, dict):
                    raise ValueError("JSON 根节点必须为对象")
            elif operation == "lookup":
                # 固化后的确定查表节点：以输入 key 从冻结的本地表选出结果。
                # 这是“探索一次，编译复用”的落点——新 key 产生不同结果，零模型调用。
                table = node.config.get("table", {})
                if not isinstance(table, dict):
                    raise ValueError("lookup 操作的 table 必须为对象")
                key_field = node.config.get("keyField", "key")
                key = inputs.get(key_field)
                default = node.config.get("default")
                output = {"value": table.get(str(key), default)}
            else:
                raise ValueError(f"不支持的转换操作: {operation}")
            if isinstance(node.config.get("outputSchema"), dict):
                Draft202012Validator(node.config["outputSchema"]).validate(output)
            return NodeResult(status="succeeded", output=output)
        except (KeyError, ValueError, TypeError, ValidationError) as exc:
            return NodeResult(
                status="failed",
                error=ExecutionError(code="INVALID_INPUT", message=str(exc)),
            )


class _SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"模板变量 {key} 不存在")


class ConditionExecutor:
    node_type = NodeType.CONDITION

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        operation = node.config.get("operation", "equals")
        value = inputs.get("value")
        expected = inputs.get("expected", node.config.get("expected"))
        if operation == "equals":
            matched = value == expected
        elif operation == "exists":
            matched = value is not None
        elif operation == "truthy":
            matched = bool(value)
        else:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="UNSUPPORTED_OPERATION",
                    message=f"不支持的条件操作: {operation}",
                ),
            )
        return NodeResult(status="succeeded", output={"result": matched})


class ApprovalExecutor:
    node_type = NodeType.APPROVAL

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        context.cancellation.check()
        snapshot = {
            "runId": context.run_id,
            "nodeId": node.id,
            "title": node.display_name,
            "description": node.config.get("description", ""),
            "payload": inputs,
            "effect": node.config.get("effect"),
        }
        return NodeResult(
            status="waiting_user",
            output={
                "approvalSnapshot": snapshot,
                "snapshotDigest": digest_json(snapshot),
            },
        )


class SkillXExecutor:
    node_type = NodeType.SKILLX

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        from app.application.skillx_execution import execute_skillx

        if node.config.get("capabilityVersionId"):
            from sqlalchemy import select

            from app.infrastructure.database import Approval, CapabilityVersion, SessionLocal

            operation = {
                "versionId": node.config["capabilityVersionId"],
                "interface": node.config["interface"],
                "input": inputs,
            }
            approved = False
            async with SessionLocal() as session:
                version = await session.get(CapabilityVersion, operation["versionId"])
                interface = version.manifest_json["interfaces"][operation["interface"]]
                required = any(
                    e.get("confirmation") == "required" for e in interface.get("effects", [])
                )
                if required:
                    approvals = await session.scalars(
                        select(Approval).where(
                            Approval.node_run_id == context.node_run_id,
                            Approval.status == "approved",
                        )
                    )
                    approved = any(
                        a.snapshot_json.get("operation") == operation
                        and a.snapshot_json.get("versionDigest") == version.digest
                        and a.snapshot_digest == digest_json(a.snapshot_json)
                        for a in approvals
                    )
                    if not approved:
                        snapshot = {
                            "runId": context.run_id,
                            "nodeId": node.id,
                            "effect": "skillx.invoke",
                            "title": "批准本次 SkillX 操作",
                            "description": "能力要求本次执行授权，请核对输入和版本。",
                            "operation": operation,
                            "payload": inputs,
                            "versionDigest": version.digest,
                        }
                        return NodeResult(
                            status="waiting_user",
                            output={
                                "approvalSnapshot": snapshot,
                                "snapshotDigest": digest_json(snapshot),
                            },
                        )

            async def cancelled():
                return context.cancellation.cancelled

            return await execute_skillx(
                node.config["capabilityVersionId"],
                node.config["interface"],
                inputs,
                context.node_run_id,
                {
                    "agentConfig": context.runtime_snapshot.get("nodeProfiles", {})
                    .get(node.id, {})
                    .get("agentConfig"),
                    "modelSnapshot": context.runtime_snapshot.get("nodeProfiles", {}).get(node.id),
                    "workingDirectory": context.runtime_snapshot.get("workingDirectory"),
                },
                cancelled,
                effect_approved=approved,
            )
        package = node.config.get("_resolvedPackage")
        interface_name = node.config.get("interface")
        if not package or not interface_name:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="MISSING_DEPENDENCY",
                    message="SkillX 节点未解析锁定的 CapabilityVersion",
                ),
            )
        try:
            secrets = node.config.get("_resolvedSecrets", {})
            result = await execute_in_subprocess(
                Path(package),
                interface_name,
                context.node_run_id,
                inputs,
                secrets=secrets,
            )
            if contains_secret(result.model_dump(), list(secrets.values())):
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="CONTRACT_VIOLATION",
                        message="SkillX 输出包含 Secret，已阻止持久化",
                    ),
                )
            return result
        except ExecutionCancelledError:
            return NodeResult(
                status="cancelled",
                error=ExecutionError(code="CANCELLED", message="SkillX 执行已取消"),
            )
        except Exception as exc:
            return NodeResult(
                status="failed",
                error=ExecutionError(code="CONTRACT_VIOLATION", message=str(exc)),
            )


class MCPExecutor:
    node_type = NodeType.MCP

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        resolved = node.config.get("_resolvedMcp")
        if not isinstance(resolved, dict):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="MISSING_DEPENDENCY",
                    message="MCP 节点未解析锁定的 ToolSnapshot",
                ),
            )
        context.cancellation.check()
        outcome = await call_mcp_contract(
            transport=resolved["transport"],
            server_config=resolved["serverConfig"],
            tool_name=resolved["toolName"],
            tool_digest=resolved["digest"],
            input_schema=resolved["inputSchema"],
            output_schema=resolved.get("outputSchema"),
            arguments=inputs,
            secret_values=resolved.get("secretValues", []),
        )
        if outcome.status == "succeeded":
            return NodeResult(
                status="succeeded",
                output=outcome.output,
                events=[
                    {
                        "type": "mcp.call.completed",
                        "payload": {
                            "toolName": outcome.tool_name,
                            "toolDigest": outcome.tool_digest,
                        },
                    }
                ],
                metrics={"mcpCallCount": 1},
            )
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code=outcome.error_code or "EXECUTION_FAILED",
                message=outcome.error_message or "MCP 调用失败",
                retryable=outcome.retryable,
            ),
        )


class LLMExecutor:
    node_type = NodeType.LLM

    @staticmethod
    def _prompt_with_resolved_inputs(prompt_template: str, inputs: dict[str, Any]) -> str:
        canonical_inputs = json.dumps(
            inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        input_section = f"[RESOLVED_INPUT_JSON]\n{canonical_inputs}\n[/RESOLVED_INPUT_JSON]"
        if "{input}" in prompt_template:
            prompt = prompt_template.replace("{input}", input_section, 1)
            return prompt.replace("{input}", "[RESOLVED_INPUT_ALREADY_INCLUDED]")
        return f"{prompt_template.rstrip()}\n\n{input_section}".lstrip()

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        output_schema = node.config.get("outputSchema")
        if not isinstance(output_schema, dict):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION",
                    message="LLM 节点缺少 outputSchema",
                ),
            )
        prompt = self._prompt_with_resolved_inputs(
            str(node.config.get("prompt", "{input}")), inputs
        )
        request_dispatched = False
        try:
            max_output_tokens = int(node.config.get("maxOutputTokens", get_settings().structured_agent_max_output_tokens))
            if node.config.get("modelProfile") == "fake":
                fake = await FakeModelAdapter(node.config.get("fakeOutput", {})).generate(
                    prompt, output_schema, max_output_tokens
                )
                output = fake.output
                input_tokens = fake.input_tokens
                output_tokens = fake.output_tokens
                model = fake.model
                runtime_used = "fake_structured"
                runtime_digest = digest_json({"profile": "fake"})
                provider = "fixture"
                usage_complete = fake.usage_complete
                tool_request_count = 0
                request_dispatched = False
            else:
                generated = await StructuredGenerationResolver().generate(
                    StructuredGenerationRequest(
                        call_id=f"structured:{context.node_run_id}",
                        prompt=prompt,
                        output_schema=output_schema,
                        max_output_tokens=max_output_tokens,
                        working_directory=(
                            get_settings().artifact_root / context.run_id / f"structured-{node.id}"
                        ),
                    ),
                    context.runtime_snapshot.get("nodeProfiles", {}).get(
                        node.id, context.runtime_snapshot
                    ),
                )
                output = generated.output
                input_tokens = generated.input_tokens
                output_tokens = generated.output_tokens
                model = generated.runtime.identity or generated.runtime.provider
                runtime_used = generated.runtime.runtime_used
                runtime_digest = generated.runtime.digest
                provider = generated.runtime.provider
                protocol = generated.runtime.protocol
                runtime_type = generated.runtime.runtime_type
                adapter_version = generated.runtime.adapter_version
                adapter_digest = generated.runtime.adapter_digest
                usage_complete = generated.usage_complete
                tool_request_count = generated.tool_request_count
                request_dispatched = generated.request_dispatched
            Draft202012Validator(output_schema).validate(output)
            return NodeResult(
                status="succeeded",
                output=output,
                metrics={
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "totalTokens": input_tokens + output_tokens,
                    "model": model,
                    "provider": provider,
                    "protocol": (
                        protocol if node.config.get("modelProfile") != "fake" else "fixture"
                    ),
                    "runtimeType": (
                        runtime_type if node.config.get("modelProfile") != "fake" else "fixture"
                    ),
                    "adapterVersion": (
                        adapter_version or "" if node.config.get("modelProfile") != "fake" else ""
                    ),
                    "adapterDigest": (
                        adapter_digest or "" if node.config.get("modelProfile") != "fake" else ""
                    ),
                    "runtimeUsed": runtime_used,
                    "runtimeDigest": runtime_digest,
                    "usageComplete": usage_complete,
                    "toolRequestCount": tool_request_count,
                    "requestDispatched": request_dispatched,
                },
                events=[
                    {
                        "type": "llm.call.completed",
                        "payload": {
                            "model": model,
                            "runtimeUsed": runtime_used,
                            "runtimeDigest": runtime_digest,
                            "inputTokens": input_tokens,
                            "outputTokens": output_tokens,
                            "toolRequestCount": tool_request_count,
                        },
                    }
                ],
            )
        except ModelRuntimeError as exc:
            snapshot = context.runtime_snapshot.get("nodeProfiles", {}).get(node.id, context.runtime_snapshot)
            runtime_selection = snapshot.get(
                "runtimeSelection",
                snapshot.get("runtimeUsed"),
            )
            metrics = (
                {
                    "provider": str(snapshot.get("provider") or "unknown"),
                    "model": str(snapshot.get("identity") or "unknown"),
                    "runtimeUsed": str(runtime_selection),
                    "runtimeDigest": str(snapshot.get("runtimeDigest") or ""),
                    "usageComplete": False,
                    "requestDispatched": exc.request_dispatched,
                }
                if runtime_selection == "agent_structured"
                else {}
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(code=exc.code, message=exc.message),
                metrics=metrics,
            )
        except Exception as exc:
            snapshot = context.runtime_snapshot.get("nodeProfiles", {}).get(node.id, context.runtime_snapshot)
            runtime_selection = snapshot.get(
                "runtimeSelection",
                snapshot.get("runtimeUsed"),
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(code="MODEL_MALFORMED_OUTPUT", message=str(exc)),
                metrics=(
                    {
                        "provider": str(snapshot.get("provider") or "unknown"),
                        "model": str(snapshot.get("identity") or "unknown"),
                        "runtimeUsed": str(runtime_selection),
                        "runtimeDigest": str(snapshot.get("runtimeDigest") or ""),
                        "usageComplete": False,
                        "requestDispatched": request_dispatched,
                    }
                    if runtime_selection == "agent_structured"
                    else {}
                ),
            )


class AgentExecutor:
    node_type = NodeType.AGENT

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        output_schema = node.config.get("outputSchema")
        if not isinstance(output_schema, dict):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION",
                    message="Agent 节点缺少 outputSchema",
                ),
            )
        if node.config.get("agentProfile") == "fake":
            output = node.config.get("fakeOutput", {})
            # echoInputField：fake Agent 回显某个输入字段作为 reply，使输出依赖真实上游数据
            # （用于动态网页总结验收：正文改变 → 总结改变）。
            echo_field = node.config.get("echoInputField")
            if echo_field and echo_field in inputs:
                output = {"reply": f"摘要：{inputs[echo_field]}"}
            try:
                Draft202012Validator(output_schema).validate(output)
            except Exception as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(code="CONTRACT_VIOLATION", message=str(exc)),
                )
            events = [
                {"type": "agent.plan", "payload": {"summary": "Fake Agent 计划"}},
                {
                    "type": "agent.step.completed",
                    "payload": {"step": 1, "kind": "fake"},
                },
                {"type": "agent.completed", "payload": {"steps": 1}},
            ]
            artifact = write_json_artifact(
                context.run_id,
                context.node_run_id,
                node.id,
                "agent-trajectory.json",
                "agent-trajectory",
                {"events": events, "output": output},
            )
            return NodeResult(
                status="succeeded",
                output=output,
                metrics={
                    "agentSteps": 1,
                    "totalTokens": 0,
                    "provider": "fixture",
                    "runtimeUsed": "fake_structured",
                    "requestDispatched": False,
                },
                events=events,
                artifacts=[artifact],
            )
        from app.application.runtime_profiles import restore_agent_snapshot

        runtime = (
            restore_agent_snapshot(
                context.runtime_snapshot.get("nodeProfiles", {}).get(node.id, {}).get("agentConfig")
            )
            or agent_runtime_config()
        )
        if not runtime.configured:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="MODEL_NOT_CONFIGURED",
                    message="TraeX Runtime 尚未配置",
                ),
                metrics={
                    "provider": "traex",
                    "runtimeUsed": "agent_runtime",
                    "requestDispatched": False,
                },
            )
        # 005-A：Workflow Agent 节点与 Conversation Agent 共享同一 AgentHost，
        # 得到一致的权限交集、事件契约、预算与取消语义；差异只在事件 sink。
        runtime_events: list[dict[str, Any]] = []
        audit_events: list[dict[str, Any]] = []

        async def event_sink(runtime_event: dict[str, Any], audit_event: dict[str, Any]) -> None:
            runtime_events.append(runtime_event)
            audit_events.append(audit_event)

        async def cancellation() -> bool:
            return context.cancellation.cancelled

        settings = get_settings()
        request = AgentHostRequest(
            run_id=context.node_run_id,
            prompt=node.config.get("prompt", "")
            + "\n输入："
            + json.dumps(inputs, ensure_ascii=False),
            output_schema=output_schema,
            working_directory=get_settings().artifact_root
            / context.run_id
            / f"agent-{context.node_run_id}",
            requested_capabilities=tuple(node.config.get("allowedCapabilities", [])),
            max_steps=int(node.config["maxSteps"]),
            max_total_tokens=max(
                int(node.config.get("maxTotalTokens", 0)),
                settings.agent_max_total_tokens,
            ),
            max_input_tokens=int(
                node.config.get("maxInputTokens", settings.agent_max_input_tokens)
            ),
            max_output_tokens=int(
                node.config.get("maxOutputTokens", settings.agent_max_output_tokens)
            ),
            max_billable_tokens=int(
                node.config.get("maxBillableTokens", settings.agent_max_billable_tokens)
            ),
            max_duration_seconds=int(node.config.get("maxDurationSeconds", node.timeout_seconds)),
        )
        try:
            result = await AgentHost(runtime=runtime).execute(
                request, event_sink=event_sink, cancellation=cancellation
            )
        except AgentHostError as exc:
            if context.cancellation.cancelled:
                return NodeResult(
                    status="cancelled",
                    error=ExecutionError(code="CANCELLED", message="Agent 执行已取消"),
                    events=audit_events,
                )
            return NodeResult(
                status="failed",
                error=ExecutionError(code=exc.code, message=exc.message),
                events=audit_events,
                metrics={
                    "provider": runtime.provider,
                    "model": runtime.model or "",
                    "protocol": runtime.protocol,
                    "runtimeType": runtime.type,
                    "adapterVersion": runtime.adapter_version,
                    "adapterDigest": runtime.adapter_digest,
                    "runtimeDigest": runtime.digest,
                    "runtimeUsed": "agent_runtime",
                    "requestDispatched": exc.request_dispatched,
                },
            )
        # 原始轨迹写受控 Artifact；数据库事件只保留脱敏审计事件。
        artifact = write_json_artifact(
            context.run_id,
            context.node_run_id,
            node.id,
            "agent-trajectory.json",
            "agent-trajectory",
            {"events": result.runtime_events, "output": result.output},
        )
        return NodeResult(
            status="succeeded",
            output=result.output,
            events=audit_events,
            metrics={
                "agentSteps": result.step_count or len(audit_events),
                "totalTokens": result.total_tokens,
                "provider": result.provider or runtime.provider,
                "model": result.model or "",
                "protocol": result.protocol or runtime.protocol,
                "runtimeType": result.runtime_type or runtime.type,
                "adapterVersion": result.adapter_version or runtime.adapter_version,
                "adapterDigest": result.adapter_digest or runtime.adapter_digest,
                "runtimeDigest": runtime.digest,
                "runtimeUsed": "agent_runtime",
                "requestDispatched": result.request_dispatched,
            },
            artifacts=[artifact],
        )


class BrowserContractExecutor:
    node_type = NodeType.BROWSER

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        # Read/Extract adapter：只读导航 + 正文/结构化字段抽取，输出作为下游数据依赖。
        if node.config.get("action") in ("read", "extract"):
            return await self._read(context, node, inputs)
        contract_data = node.config.get("pageContract")
        if not contract_data:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION",
                    message="Browser 节点缺少 pageContract",
                ),
            )
        contract = PageContract.model_validate(contract_data)
        if node.config.get("runtime") == "playwright":
            url = node.config.get("url")
            if not isinstance(url, str):
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="CONTRACT_VIOLATION",
                        message="Playwright Browser 节点缺少 url",
                    ),
                )
            try:
                navigation_timeout_ms = min(
                    int(node.config.get("navigationTimeoutMs", 15000)),
                    max(100, node.timeout_seconds * 1000 - 250),
                )
                async with PlaywrightPageSession(
                    context.run_id,
                    node.id,
                    url,
                    contract,
                    {
                        **node.config,
                        "navigationTimeoutMs": navigation_timeout_ms,
                    },
                    context.node_run_id,
                ) as browser:
                    context.cancellation.check()
                    observed = await browser.inspect_and_fill(inputs.get("formValues", {}))
                    return await self._evaluate(
                        context,
                        node,
                        inputs,
                        contract,
                        observed,
                        browser,
                    )
            except PlaywrightTimeoutError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="TIMEOUT",
                        message=f"Playwright 导航超时: {exc}",
                        retryable=not node.side_effect,
                    ),
                )
            except BrowserTransportError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="DEPENDENCY_UNAVAILABLE",
                        message=f"Playwright transport 失败: {exc}",
                        retryable=not node.side_effect,
                    ),
                )
            except BrowserExecutionError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="EXECUTION_FAILED",
                        message=str(exc),
                    ),
                )
            except PlaywrightError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="DEPENDENCY_UNAVAILABLE",
                        message=f"Playwright 执行失败: {exc}",
                        retryable=not node.side_effect,
                    ),
                )
        observed_data = inputs.get("observedPage")
        if not observed_data:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="UNSUPPORTED_OPERATION",
                    message="Browser 测试 Adapter 需要 observedPage 输入",
                ),
            )
        return await self._evaluate(
            context,
            node,
            inputs,
            contract,
            ObservedPage.model_validate(observed_data),
            None,
        )

    async def _read(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        """Browser Read/Extract：导航 URL，抽取正文与结构化字段，按 outputSchema 校验。

        - 真实 runtime=playwright：受控 Browser Executor 导航并读取；
        - 测试 adapter：从 ``inputs.observedContent`` 读取正文/字段，编译期契约不变。
        URL 来自输入（``${input.url}`` 等绑定），因此第二次运行用新输入重新取数，
        不使用首次结果或常量正文。
        """

        contract_data = node.config.get("pageContract")
        if not isinstance(contract_data, dict):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION", message="Browser Read 节点缺少 pageContract"
                ),
            )
        contract = BrowserReadContract.model_validate(contract_data)
        url = inputs.get("url") or node.config.get("url")
        result: dict[str, Any]
        if node.config.get("runtime") == "playwright":
            if not isinstance(url, str):
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="CONTRACT_VIOLATION", message="Browser Read 节点缺少 url"
                    ),
                )
            # 复用 PlaywrightPageSession 的导航/安全边界（url_matches 检查）。
            page_contract = PageContract(
                contractVersion=contract.contract_version,
                urlPattern=contract.url_pattern,
                fields=[],
            )
            navigation_timeout_ms = min(
                contract.timeout_ms,
                max(100, node.timeout_seconds * 1000 - 250),
            )
            try:
                async with PlaywrightPageSession(
                    context.run_id,
                    node.id,
                    url,
                    page_contract,
                    {
                        **node.config,
                        "navigationTimeoutMs": navigation_timeout_ms,
                    },
                    context.node_run_id,
                ) as browser:
                    context.cancellation.check()
                    result = await browser.read_and_extract(contract.body_selector, contract.fields)
                    artifacts = browser.artifacts
            except PlaywrightTimeoutError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="TIMEOUT",
                        message=f"Playwright 导航超时: {exc}",
                        retryable=True,
                    ),
                )
            except BrowserTransportError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="DEPENDENCY_UNAVAILABLE",
                        message=f"Playwright 读取失败: {exc}",
                        retryable=contract.retryable,
                    ),
                )
            except BrowserExecutionError as exc:
                return NodeResult(
                    status="failed",
                    output={"url": url},
                    error=ExecutionError(code="PAGE_DRIFT", message=str(exc)),
                )
            except PlaywrightError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="DEPENDENCY_UNAVAILABLE",
                        message=f"Playwright transport 失败: {exc}",
                        retryable=contract.retryable,
                    ),
                )
        else:
            observed = inputs.get("observedContent")
            if not isinstance(observed, dict):
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="UNSUPPORTED_OPERATION",
                        message="Browser Read 测试 Adapter 需要 observedContent 输入",
                    ),
                )
            if isinstance(url, str) and not url_matches(contract.url_pattern, url):
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="PAGE_DRIFT", message="目标 URL 不符合 BrowserReadContract"
                    ),
                )
            result = {
                "url": url or observed.get("url", ""),
                "title": observed.get("title", ""),
                "content": observed.get("content", ""),
                "fields": observed.get("fields", {}),
            }
            artifacts = []
        if contract.output_schema:
            errors = list(Draft202012Validator(contract.output_schema).iter_errors(result))
            if errors:
                return NodeResult(
                    status="failed",
                    output=result,
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="CONTRACT_VIOLATION",
                        message=f"Browser 抽取输出不符合契约: {errors[0].message}",
                    ),
                )
        return NodeResult(
            status="succeeded",
            output=result,
            artifacts=artifacts,
            events=[{"type": "browser.read.completed", "payload": {"url": result["url"]}}],
        )

    async def _evaluate(
        self,
        context: RunContext,
        node: WorkflowNode,
        inputs: dict[str, Any],
        contract: PageContract,
        observed: ObservedPage,
        browser: PlaywrightPageSession | None,
    ) -> NodeResult:
        drift = detect_drift(
            contract,
            observed,
        )
        observed_data = observed.model_dump(by_alias=True)
        artifacts = browser.artifacts if browser is not None else []
        output = {
            "drift": drift.model_dump(),
            "formValues": inputs.get("formValues", {}),
        }
        if drift.level == "L4":
            return NodeResult(
                status="failed",
                artifacts=artifacts,
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面存在安全或身份异常",
                    details=drift.model_dump(),
                ),
            )
        if drift.level == "L3":
            # L3 只生成 ContractPatch 草稿与差异说明，不修改当前 WorkflowVersion/PageContract。
            patch_payload: dict[str, Any] | None = None
            try:
                patch_payload = build_contract_patch(
                    contract, observed, drift.differences
                ).to_dict()
            except ContractPatchViolation as exc:
                patch_payload = {"status": "rejected", "reason": str(exc)}
            return NodeResult(
                status="failed",
                output={**output, "contractPatch": patch_payload},
                artifacts=artifacts,
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面结构性变化已阻止当前版本，已生成 ContractPatch 草稿待人工审核",
                    details={**drift.model_dump(), "contractPatch": patch_payload},
                ),
            )
        if drift.level == "L2":
            form_values = inputs.get("formValues", {})
            fingerprint = observed.fingerprint or digest_json(observed_data)
            current_drift_digest = drift_authorization_digest(
                context.run_id,
                observed.url,
                observed.identity,
                observed.business_object,
                fingerprint,
                form_values,
                drift.differences,
            )
            approved_drift = inputs.get("_driftAuthorization")
            if approved_drift != current_drift_digest:
                snapshot = self._drift_snapshot(
                    context,
                    node,
                    observed,
                    form_values,
                    drift.model_dump(),
                    current_drift_digest,
                )
                message = (
                    "页面存在可解释变化，请确认是否允许重新评估并继续"
                    if approved_drift is None
                    else "页面在授权后再次变化，原漂移授权失效，请重新确认"
                )
                return NodeResult(
                    status="waiting_user",
                    output={
                        **output,
                        "approvalSnapshot": snapshot,
                        "snapshotDigest": digest_json(snapshot),
                    },
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="PAGE_DRIFT",
                        message=message,
                        details=drift.model_dump(),
                    ),
                )
            # 漂移已获得一次性授权，继续按只读或提交语义处理当前页面。
        if node.config.get("submit"):
            form_values = inputs.get("formValues", {})
            fingerprint = observed.fingerprint or digest_json(observed_data)
            current_digest = authorization_digest(
                context.run_id,
                fingerprint,
                form_values,
                observed.identity,
                contract.submit,
            )
            approved_digest = inputs.get("_authorizationDigest")
            if not approved_digest:
                snapshot = self._authorization_snapshot(
                    context,
                    node,
                    observed,
                    form_values,
                    drift.model_dump(),
                    current_digest,
                )
                return NodeResult(
                    status="waiting_user",
                    output={
                        **output,
                        "approvalSnapshot": snapshot,
                        "snapshotDigest": digest_json(snapshot),
                    },
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="CONFIRMATION_REQUIRED",
                        message="浏览器提交等待一次性授权",
                    ),
                )
            if approved_digest != current_digest:
                snapshot = self._authorization_snapshot(
                    context,
                    node,
                    observed,
                    form_values,
                    drift.model_dump(),
                    current_digest,
                )
                return NodeResult(
                    status="waiting_user",
                    output={
                        **output,
                        "approvalSnapshot": snapshot,
                        "snapshotDigest": digest_json(snapshot),
                    },
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="CONFIRMATION_REQUIRED",
                        message="页面、身份或表单数据已变化，原授权失效",
                    ),
                )
            try:
                if browser is not None:
                    context.cancellation.check()
                    submit_result = await browser.submit()
                else:
                    submit_result = node.config.get("fakeSubmitResult")
            except BrowserSubmitUnknownOutcome as exc:
                snapshot = {
                    "runId": context.run_id,
                    "nodeId": node.id,
                    "title": f"{node.display_name}提交结果未知",
                    "description": str(exc),
                    "payload": {
                        "target": observed.url,
                        "identity": observed.identity,
                    },
                    "effect": "form.submit.unknown_outcome",
                }
                return NodeResult(
                    status="waiting_user",
                    output={
                        **output,
                        "approvalSnapshot": snapshot,
                        "snapshotDigest": digest_json(snapshot),
                    },
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="UNKNOWN_OUTCOME",
                        message=str(exc),
                    ),
                )
            if not isinstance(submit_result, dict):
                return NodeResult(
                    status="failed",
                    artifacts=artifacts,
                    error=ExecutionError(
                        code="UNSUPPORTED_OPERATION",
                        message="Browser 测试 Adapter 未配置提交结果",
                    ),
                )
            return NodeResult(
                status="succeeded",
                output={
                    **submit_result,
                    "evidence": {
                        "authorizationDigest": current_digest,
                        "pageFingerprint": fingerprint,
                    },
                },
                artifacts=artifacts,
                events=[
                    {
                        "type": "browser.submit.completed",
                        "payload": {"target": observed.url},
                    }
                ],
            )
        return NodeResult(status="succeeded", output=output, artifacts=artifacts)

    @staticmethod
    def _drift_snapshot(
        context: RunContext,
        node: WorkflowNode,
        observed: ObservedPage,
        form_values: dict[str, Any],
        drift: dict[str, Any],
        drift_digest: str,
    ) -> dict[str, Any]:
        return {
            "runId": context.run_id,
            "nodeId": node.id,
            "title": f"确认页面变化 {node.display_name}",
            "description": "页面存在可解释变化。批准仅代表允许重新评估并继续，不代表动作已经完成。",
            "payload": {
                "target": observed.url,
                "identity": observed.identity,
                "businessObject": observed.business_object,
                "formValues": form_values,
                "drift": drift,
            },
            "effect": "browser.drift",
            "driftAuthorization": drift_digest,
        }

    @staticmethod
    def _authorization_snapshot(
        context: RunContext,
        node: WorkflowNode,
        observed: ObservedPage,
        form_values: dict[str, Any],
        drift: dict[str, Any],
        current_digest: str,
    ) -> dict[str, Any]:
        return {
            "runId": context.run_id,
            "nodeId": node.id,
            "title": f"确认提交 {node.display_name}",
            "description": "请核对页面、身份和表单数据后授权一次提交。",
            "payload": {
                "target": observed.url,
                "identity": observed.identity,
                "formValues": form_values,
                "drift": drift,
            },
            "effect": "form.submit",
            "authorizationDigest": current_digest,
        }


class ArtifactExecutor:
    node_type = NodeType.ARTIFACT

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        context.cancellation.check()
        name = Path(node.config.get("name", f"{node.id}.json")).name
        media_type = node.config.get("mediaType", "application/json")
        artifact_id = str(uuid4())
        # 先写暂存目录；CAS 成功后由编排器原子落位并登记。
        staging = _staging_dir(context.run_id, context.node_run_id)
        staging.mkdir(parents=True, exist_ok=True)
        target = (staging / artifact_id).resolve()
        if not target.is_relative_to(staging):
            return NodeResult(
                status="failed",
                error=ExecutionError(code="PERMISSION_DENIED", message="Artifact 路径越界"),
            )
        if media_type == "application/json":
            content = json.dumps(inputs, ensure_ascii=False, indent=2).encode()
        else:
            content = str(inputs.get("content", "")).encode()
        target.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        artifact = {
            "id": artifact_id,
            "kind": node.config.get("kind", "report"),
            "name": name,
            "uri": f"artifact://{artifact_id}",
            "sha256": sha256,
            "size": len(content),
            "mediaType": media_type,
            "storageKey": artifact_id,
            "stagingKey": {"nodeRunId": context.node_run_id, "file": artifact_id},
            "metadata": {"nodeId": node.id},
        }
        return NodeResult(
            status="succeeded",
            output={
                "artifact": {
                    key: value
                    for key, value in artifact.items()
                    if key not in ("storageKey", "stagingKey")
                }
            },
            artifacts=[artifact],
        )


class UnconfiguredExecutor:
    def __init__(self, node_type: NodeType) -> None:
        self.node_type = node_type

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="UNSUPPORTED_OPERATION",
                message=f"{self.node_type.value} Runtime 尚未配置",
                details={"nodeId": node.id},
            ),
        )


class ExecutorRegistry:
    def __init__(self) -> None:
        executors: list[NodeExecutor] = [
            InputExecutor(),
            TransformExecutor(),
            ConditionExecutor(),
            ApprovalExecutor(),
            OutputExecutor(),
            SkillXExecutor(),
            MCPExecutor(),
            LLMExecutor(),
            AgentExecutor(),
            BrowserContractExecutor(),
            ArtifactExecutor(),
        ]
        self._executors = {executor.node_type: executor for executor in executors}

    def get(self, node_type: NodeType) -> NodeExecutor:
        return self._executors[node_type]
