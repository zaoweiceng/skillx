from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.config import get_settings
from app.domain.workflow import ExecutionError, NodeResult, NodeType, WorkflowNode
from app.infrastructure.database import Approval, Artifact, NodeRun, Run
from app.runtime.browser import ObservedPage, PageContract, detect_drift
from app.runtime.skillx import execute_direct, load_interface


@dataclass
class RunContext:
    session: AsyncSession
    run: Run
    node_run: NodeRun


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
        return NodeResult(status="succeeded", output=context.run.input_json)


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
        snapshot = {
            "runId": context.run.id,
            "nodeId": node.id,
            "title": node.display_name,
            "description": node.config.get("description", ""),
            "payload": inputs,
            "effect": node.config.get("effect"),
        }
        approval = Approval(
            id=str(uuid4()),
            run_id=context.run.id,
            node_run_id=context.node_run.id,
            node_id=node.id,
            snapshot_digest=digest_json(snapshot),
            snapshot_json=snapshot,
            status="pending",
        )
        context.session.add(approval)
        await context.session.flush()
        return NodeResult(
            status="waiting_user",
            output={"approvalId": approval.id, "snapshotDigest": approval.snapshot_digest},
            resume_token=approval.id,
        )


class SkillXExecutor:
    node_type = NodeType.SKILLX

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        package = node.config.get("package")
        interface_name = node.config.get("interface")
        if not package or not interface_name:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="MISSING_DEPENDENCY",
                    message="SkillX 节点缺少 package 或 interface 配置",
                ),
            )
        try:
            loaded = load_interface(Path(package), interface_name)
            return await execute_direct(loaded, context.node_run.id, inputs)
        except Exception as exc:
            return NodeResult(
                status="failed",
                error=ExecutionError(code="CONTRACT_VIOLATION", message=str(exc)),
            )


class BrowserContractExecutor:
    node_type = NodeType.BROWSER

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        contract = node.config.get("pageContract")
        observed = inputs.get("observedPage")
        if not contract or not observed:
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="UNSUPPORTED_OPERATION",
                    message="未配置浏览器 Adapter；契约检查需要 observedPage 输入",
                ),
            )
        drift = detect_drift(
            PageContract.model_validate(contract),
            ObservedPage.model_validate(observed),
        )
        output = {"drift": drift.model_dump(), "formValues": inputs.get("formValues", {})}
        if drift.level == "L2":
            snapshot = {
                "runId": context.run.id,
                "nodeId": node.id,
                "title": f"{node.display_name}页面变化",
                "description": "页面存在可解释变化，请确认是否继续本次只读步骤。",
                "payload": output,
                "effect": "browser.read",
            }
            approval = Approval(
                id=str(uuid4()),
                run_id=context.run.id,
                node_run_id=context.node_run.id,
                node_id=node.id,
                snapshot_digest=digest_json(snapshot),
                snapshot_json=snapshot,
                status="pending",
            )
            context.session.add(approval)
            await context.session.flush()
            return NodeResult(
                status="waiting_user",
                output={
                    **output,
                    "approvalId": approval.id,
                    "snapshotDigest": approval.snapshot_digest,
                },
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面发生变化，需要用户处理",
                    details=drift.model_dump(),
                ),
            )
        if drift.level == "L3":
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面发生结构性变化，禁止继续当前发布版本",
                    details=drift.model_dump(),
                ),
            )
        if drift.level == "L4":
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="PAGE_DRIFT",
                    message="页面存在安全或身份异常",
                    details=drift.model_dump(),
                ),
            )
        if node.config.get("submit"):
            return NodeResult(
                status="failed",
                error=ExecutionError(
                    code="CONFIRMATION_REQUIRED",
                    message="浏览器提交必须通过专用 Adapter 验证一次性授权",
                ),
            )
        return NodeResult(status="succeeded", output=output)


class ArtifactExecutor:
    node_type = NodeType.ARTIFACT

    async def execute(
        self, context: RunContext, node: WorkflowNode, inputs: dict[str, Any]
    ) -> NodeResult:
        settings = get_settings()
        name = Path(node.config.get("name", f"{node.id}.json")).name
        media_type = node.config.get("mediaType", "application/json")
        run_root = (settings.artifact_root / context.run.id).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        target = (run_root / name).resolve()
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
        artifact = Artifact(
            id=str(uuid4()),
            run_id=context.run.id,
            kind=node.config.get("kind", "report"),
            name=name,
            uri=f"artifact://{context.run.id}/{name}",
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type=media_type,
            metadata_json={"nodeId": node.id},
        )
        context.session.add(artifact)
        await context.session.flush()
        return NodeResult(
            status="succeeded",
            output={
                "artifact": {
                    "id": artifact.id,
                    "name": artifact.name,
                    "uri": artifact.uri,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                    "mediaType": artifact.media_type,
                }
            },
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
            BrowserContractExecutor(),
            ArtifactExecutor(),
            UnconfiguredExecutor(NodeType.MCP),
            UnconfiguredExecutor(NodeType.LLM),
            UnconfiguredExecutor(NodeType.AGENT),
        ]
        self._executors = {executor.node_type: executor for executor in executors}

    def get(self, node_type: NodeType) -> NodeExecutor:
        return self._executors[node_type]
