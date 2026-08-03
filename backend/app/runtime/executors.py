from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from jsonschema import Draft202012Validator

from app.application.compiler import digest_json
from app.application.mcp import open_gateway
from app.application.secrets import contains_secret, redact_secret_text
from app.application.security import redact
from app.config import get_settings
from app.domain.workflow import ExecutionError, NodeResult, NodeType, WorkflowNode
from app.runtime.adapters import AdapterError, AgentRunRequest, TraeXSubprocessAdapter
from app.runtime.browser import (
    ObservedPage,
    PageContract,
    authorization_digest,
    detect_drift,
)
from app.runtime.models import FakeModelAdapter, OpenAICompatibleAdapter
from app.runtime.playwright_browser import (
    BrowserExecutionError,
    BrowserSubmitUnknownOutcome,
    PlaywrightPageSession,
)
from app.runtime.skillx_host import execute_in_subprocess


class ExecutionCancelledError(Exception):
    pass


def write_json_artifact(
    run_id: str,
    node_id: str,
    name: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = str(uuid4())
    root = (get_settings().artifact_root / run_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / artifact_id).resolve()
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
        "metadata": {"nodeId": node_id},
    }


@dataclass
class CancellationToken:
    event: asyncio.Event

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise ExecutionCancelledError("执行已取消")


@dataclass
class RunContext:
    run_id: str
    run_input: dict[str, Any]
    node_run_id: str
    cancellation: CancellationToken


class NodeExecutor(Protocol):
    node_type: NodeType

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult: ...


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
            else:
                raise ValueError(f"不支持的转换操作: {operation}")
            return NodeResult(status="succeeded", output=output)
        except (KeyError, ValueError, TypeError) as exc:
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
        errors = list(Draft202012Validator(resolved["inputSchema"]).iter_errors(inputs))
        if errors:
            return NodeResult(
                status="failed",
                error=ExecutionError(code="INVALID_INPUT", message=errors[0].message),
            )
        context.cancellation.check()
        try:
            async with open_gateway(resolved["transport"], resolved["serverConfig"]) as gateway:
                raw = await gateway.call(resolved["toolName"], inputs)
            output = self._normalize_output(raw)
            if contains_secret(output, resolved.get("secretValues", [])):
                raise ValueError("MCP Tool 输出包含 Secret，已阻止持久化")
            if resolved.get("outputSchema"):
                Draft202012Validator(resolved["outputSchema"]).validate(output)
            return NodeResult(
                status="succeeded",
                output=output,
                events=[
                    {
                        "type": "mcp.call.completed",
                        "payload": {
                            "toolName": resolved["toolName"],
                            "toolDigest": resolved["digest"],
                        },
                    }
                ],
                metrics={"mcpCallCount": 1},
            )
        except AdapterError as exc:
            message = redact_secret_text(
                str(exc),
                resolved.get("secretValues", []),
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="DEPENDENCY_UNAVAILABLE",
                    message=message,
                    retryable=True,
                ),
            )
        except Exception as exc:
            message = redact_secret_text(
                str(exc),
                resolved.get("secretValues", []),
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONTRACT_VIOLATION",
                    message=f"MCP Tool 输出不符合契约: {message}",
                ),
            )

    @staticmethod
    def _normalize_output(raw: dict[str, Any]) -> dict[str, Any]:
        structured = raw.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in raw.get("content", []):
            if item.get("type") == "text":
                parsed = json.loads(item.get("text", "{}"))
                if isinstance(parsed, dict):
                    return parsed
        if isinstance(raw, dict) and "content" not in raw:
            return raw
        raise ValueError("MCP Tool 未返回结构化对象")


class LLMExecutor:
    node_type = NodeType.LLM

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
        prompt = node.config.get("prompt", "{input}").replace(
            "{input}", json.dumps(inputs, ensure_ascii=False)
        )
        adapter = (
            FakeModelAdapter(node.config.get("fakeOutput", {}))
            if node.config.get("modelProfile") == "fake"
            else OpenAICompatibleAdapter()
        )
        try:
            result = await adapter.generate(
                prompt,
                output_schema,
                int(node.config.get("maxOutputTokens", 1000)),
            )
            Draft202012Validator(output_schema).validate(result.output)
            return NodeResult(
                status="succeeded",
                output=result.output,
                metrics={
                    "inputTokens": result.input_tokens,
                    "outputTokens": result.output_tokens,
                    "model": result.model,
                },
                events=[
                    {
                        "type": "llm.call.completed",
                        "payload": {
                            "model": result.model,
                            "inputTokens": result.input_tokens,
                            "outputTokens": result.output_tokens,
                        },
                    }
                ],
            )
        except Exception as exc:
            code = (
                "MODEL_NOT_CONFIGURED"
                if "MODEL_NOT_CONFIGURED" in str(exc)
                else "CONTRACT_VIOLATION"
            )
            return NodeResult(
                status="failed",
                error=ExecutionError(code=code, message=str(exc)),
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
                node.id,
                "agent-trajectory.json",
                "agent-trajectory",
                {"events": events, "output": output},
            )
            return NodeResult(
                status="succeeded",
                output=output,
                metrics={"agentSteps": 1, "totalTokens": 0},
                events=events,
                artifacts=[artifact],
            )
        settings = get_settings()
        if not settings.traex_command:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="MODEL_NOT_CONFIGURED",
                    message="TraeX Runtime 尚未配置",
                ),
            )
        adapter = TraeXSubprocessAdapter(
            settings.traex_command,
            set(settings.traex_allowed_executables),
        )
        events: list[dict[str, Any]] = []
        output: dict[str, Any] | None = None
        request = AgentRunRequest(
            prompt=node.config.get("prompt", "")
            + "\n输入："
            + json.dumps(inputs, ensure_ascii=False),
            working_directory=(get_settings().artifact_root / context.run_id / f"agent-{node.id}"),
            allowed_tools=node.config.get("allowedCapabilities", []),
            max_steps=int(node.config["maxSteps"]),
            max_tokens=int(node.config.get("maxTotalTokens", 30000)),
        )
        try:
            async for event in adapter.events(context.node_run_id, request):
                event_type = event.get("type", "log.emitted")
                payload = event.get("payload", {})
                events.append({"type": event_type, "payload": payload})
                if event_type == "agent.completed":
                    output = payload.get("output")
            if output is None:
                raise ValueError("Agent 未返回结构化输出")
            Draft202012Validator(output_schema).validate(output)
            artifact = write_json_artifact(
                context.run_id,
                node.id,
                "agent-trajectory.json",
                "agent-trajectory",
                {"events": events, "output": output},
            )
            return NodeResult(
                status="succeeded",
                output=output,
                events=events,
                metrics={"agentSteps": len(events)},
                artifacts=[artifact],
            )
        except Exception as exc:
            return NodeResult(
                status="failed",
                error=ExecutionError(code="EXECUTION_FAILED", message=str(exc)),
                events=events,
            )


class BrowserContractExecutor:
    node_type = NodeType.BROWSER

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
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
                async with PlaywrightPageSession(
                    context.run_id,
                    node.id,
                    url,
                    contract,
                    node.config,
                ) as browser:
                    context.cancellation.check()
                    observed = await browser.inspect_and_fill(
                        inputs.get("formValues", {})
                    )
                    return await self._evaluate(
                        context,
                        node,
                        inputs,
                        contract,
                        observed,
                        browser,
                    )
            except BrowserExecutionError as exc:
                return NodeResult(
                    status="failed",
                    error=ExecutionError(
                        code="EXECUTION_FAILED",
                        message=str(exc),
                    ),
                )
            except Exception as exc:
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
        if drift.level == "L2":
            snapshot = {
                "runId": context.run_id,
                "nodeId": node.id,
                "title": f"{node.display_name}页面变化",
                "description": "页面存在可解释变化，请确认是否继续本次只读步骤。",
                "payload": output,
                "effect": "browser.read",
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
                    code="PAGE_DRIFT",
                    message="页面发生变化，需要用户处理",
                    details=drift.model_dump(),
                ),
            )
        if drift.level == "L3":
            return NodeResult(
                status="failed",
                artifacts=artifacts,
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面发生结构性变化，禁止继续当前发布版本",
                    details=drift.model_dump(),
                ),
            )
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
        settings = get_settings()
        context.cancellation.check()
        name = Path(node.config.get("name", f"{node.id}.json")).name
        media_type = node.config.get("mediaType", "application/json")
        artifact_id = str(uuid4())
        run_root = (settings.artifact_root / context.run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        storage_key = artifact_id
        target = (run_root / storage_key).resolve()
        if not target.is_relative_to(run_root):
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
            "storageKey": storage_key,
            "metadata": {"nodeId": node.id},
        }
        return NodeResult(
            status="succeeded",
            output={
                "artifact": {key: value for key, value in artifact.items() if key != "storageKey"}
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
