from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_run import (
    AgentRunConfig,
    enqueue_agent_run,
    request_cancellation,
    summarize_run,
)
from app.application.capabilities import (
    CapabilityResolutionError,
    register_skillx,
    validate_workflow_capabilities,
)
from app.application.compiler import WorkflowCompiler, WorkflowValidationError, digest_json
from app.application.conversation import (
    ComponentCandidate,
    candidate_message,
    candidate_snapshot_digest,
    match_component,
)
from app.application.distillation import (
    DistillationError,
    distill_conversation_run,
)
from app.application.mcp import (
    MCPConfigurationError,
    MCPContractDriftError,
    call_snapshot_through_contract,
    classify_snapshot,
    discover_tools,
    register_server,
    scan_dependent_workflows,
    validate_mcp_nodes,
)
from app.application.orchestrator import Orchestrator, append_event
from app.application.retries import auto_retry_allowed
from app.application.secrets import (
    SecretResolutionError,
    register_secret_reference,
)
from app.application.security import redact
from app.application.workflow_patch import (
    WorkflowPatch,
    WorkflowPatchViolation,
    apply_workflow_patch,
)
from app.config import get_settings
from app.infrastructure.database import (
    Approval,
    Artifact,
    Capability,
    Component,
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    Distillation,
    ExecutionCheckpoint,
    MCPServer,
    MCPToolSnapshot,
    Message,
    NodeRun,
    Run,
    RunEvent,
    SecretReference,
    Workflow,
    WorkflowVersion,
    get_session,
    utcnow,
)
from app.infrastructure.migrations import alembic_head_revision
from app.runtime.contract_patch import ContractPatchViolation, apply_contract_patch
from app.runtime.models import OpenAICompatibleAdapter

router = APIRouter()
compiler = WorkflowCompiler()
orchestrator = Orchestrator()


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]


class DraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    definition: dict[str, Any]


class ComponentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    workflow_version_id: str = Field(alias="workflowVersionId")
    visibility: str = "private"


class RunCreate(BaseModel):
    input: dict[str, Any]


class Decision(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")


class CapabilityCreate(BaseModel):
    name: str
    type: str
    package_path: str | None = Field(default=None, alias="packagePath")
    interface: str | None = None


class MCPServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: str
    config: dict[str, Any]


class MCPToolTest(BaseModel):
    arguments: dict[str, Any]


class MCPToolClassify(BaseModel):
    effect: str
    idempotent: bool


class SecretReferenceCreate(BaseModel):
    name: str
    environment_variable: str = Field(alias="environmentVariable")
    description: str = ""


class ConversationCreate(BaseModel):
    title: str = "新对话"
    bound_component_id: str | None = Field(default=None, alias="boundComponentId")


class MessageCreate(BaseModel):
    content: str
    component_id: str | None = Field(default=None, alias="componentId")
    inputs: dict[str, Any] | None = None


class ComponentConfirm(BaseModel):
    component_id: str = Field(alias="componentId")
    candidate_snapshot_digest: str = Field(alias="candidateSnapshotDigest")
    inputs: dict[str, Any] = Field(default_factory=dict)


class ConversationRunCreate(BaseModel):
    input: dict[str, Any]
    output: dict[str, Any]
    trajectory: dict[str, Any]


class ContractPatchApply(BaseModel):
    node_id: str = Field(alias="nodeId")
    patch: dict[str, Any]


class AgentRunCreate(BaseModel):
    content: str = Field(min_length=1)
    allowed_capabilities: list[str] | None = Field(default=None, alias="allowedCapabilities")
    max_steps: int | None = Field(default=None, alias="maxSteps", ge=1, le=32)


def workflow_view(item: Workflow) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "draftRevision": item.draft_revision,
        "definition": item.draft_json,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


def run_view(
    run: Run,
    node_runs: list[NodeRun] | None = None,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": run.id,
        "workflowVersionId": run.workflow_version_id,
        "componentId": run.component_id,
        "status": run.status,
        "input": redact(run.input_json, input_schema),
        "output": redact(run.output_json, output_schema),
        "error": redact(run.error_json),
        "createdAt": run.created_at,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
    }
    if node_runs is not None:
        payload["nodes"] = [
            {
                "id": item.id,
                "nodeId": item.node_id,
                "type": item.node_type,
                "attempt": item.attempt,
                "status": item.status,
                "input": redact(item.input_json),
                "output": redact(item.output_json),
                "error": redact(item.error_json),
                "startedAt": item.started_at,
                "finishedAt": item.finished_at,
                "durationMs": _duration_ms(item.started_at, item.finished_at),
            }
            for item in node_runs
        ]
    return payload


def _duration_ms(started: Any, finished: Any) -> int | None:
    if started is None or finished is None:
        return None
    return int((finished - started).total_seconds() * 1000)


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        expected = alembic_head_revision()
        if revision != expected:
            raise RuntimeError(f"数据库迁移版本应为 {expected}，实际为 {revision}")
        await session.execute(text("CREATE TEMP TABLE IF NOT EXISTS readiness_probe (id INTEGER)"))
        root = get_settings().artifact_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / f".ready-{uuid4()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        raise HTTPException(
            503,
            detail={"code": "NOT_READY", "message": str(exc)},
        ) from exc
    return {"status": "ready", "migration": revision}


@router.get("/workflows")
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Workflow).order_by(Workflow.updated_at.desc()))).all()
    return [workflow_view(item) for item in rows]


@router.post("/workflows", status_code=201)
async def create_workflow(
    body: WorkflowCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        compiler.compile(body.definition)
    except WorkflowValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "WORKFLOW_VALIDATION_FAILED", "diagnostics": exc.diagnostics},
        ) from exc
    item = Workflow(
        id=str(uuid4()),
        name=body.name,
        draft_revision=1,
        draft_json=body.definition,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return workflow_view(item)


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    return workflow_view(item)


@router.put("/workflows/{workflow_id}/draft")
async def update_draft(
    workflow_id: str,
    body: DraftUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    if item.draft_revision != body.revision:
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_REVISION_CONFLICT",
                "currentRevision": item.draft_revision,
            },
        )
    item.draft_json = body.definition
    item.draft_revision += 1
    await session.commit()
    await session.refresh(item)
    return workflow_view(item)


@router.post("/workflows/{workflow_id}/validate")
async def validate_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    try:
        compiled = compiler.compile(item.draft_json)
        execution_class, capability_locks = await validate_workflow_capabilities(
            session, compiled.definition
        )
        mcp_locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
        return {
            "valid": True,
            "digest": compiled.digest,
            "topologicalOrder": compiled.topological_order,
            "diagnostics": compiled.diagnostics,
            "executionClass": execution_class,
            "capabilityLocks": capability_locks,
            "mcpLocks": mcp_locks,
        }
    except WorkflowValidationError as exc:
        return {"valid": False, "diagnostics": exc.diagnostics}
    except CapabilityResolutionError as exc:
        return {
            "valid": False,
            "diagnostics": [
                {
                    "level": "error",
                    "code": "CAPABILITY_INCOMPATIBLE",
                    "path": "spec.nodes",
                    "message": str(exc),
                }
            ],
        }
    except MCPConfigurationError as exc:
        return {
            "valid": False,
            "diagnostics": [
                {
                    "level": "error",
                    "code": "MCP_TOOL_INCOMPATIBLE",
                    "path": "spec.nodes",
                    "message": str(exc),
                }
            ],
        }


@router.post("/workflows/{workflow_id}/publish", status_code=201)
async def publish_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    try:
        compiled = compiler.compile(item.draft_json)
        execution_class, capability_locks = await validate_workflow_capabilities(
            session, compiled.definition
        )
        mcp_locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_VALIDATION_FAILED", "diagnostics": exc.diagnostics},
        ) from exc
    except (CapabilityResolutionError, MCPConfigurationError) as exc:
        raise HTTPException(
            422,
            detail={"code": "CAPABILITY_INCOMPATIBLE", "message": str(exc)},
        ) from exc
    latest = await session.scalar(
        select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == item.id)
    )
    version = WorkflowVersion(
        id=str(uuid4()),
        workflow_id=item.id,
        version=(latest or 0) + 1,
        definition_json=compiled.definition.model_dump(by_alias=True, mode="json"),
        digest=compiled.digest,
        validation_json={
            "topologicalOrder": compiled.topological_order,
            "diagnostics": [item.model_dump() for item in compiled.diagnostics],
            "executionClass": execution_class,
            "capabilityLocks": capability_locks,
            "mcpLocks": mcp_locks,
        },
    )
    session.add(version)
    await session.commit()
    await session.refresh(version)
    return {
        "id": version.id,
        "workflowId": version.workflow_id,
        "version": version.version,
        "digest": version.digest,
        "definition": version.definition_json,
    }


@router.post("/workflows/{workflow_id}/contract-patch", status_code=200)
async def apply_workflow_contract_patch(
    workflow_id: str,
    body: ContractPatchApply,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把人工审核过的 ContractPatch 应用到工作流草稿（不触碰已发布版本）。

    仅调整目标 browser 节点 pageContract 的字段契约并 +1 版本号；URL/身份/提交动作等
    L4 安全域字段不可通过补丁修改。应用后仍需人工在编辑器中校验并显式发布。
    """

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, "工作流不存在")
    try:
        patched = apply_contract_patch(workflow.draft_json, body.node_id, body.patch)
    except ContractPatchViolation as exc:
        raise HTTPException(
            422,
            detail={"code": "CONTRACT_PATCH_REJECTED", "message": str(exc)},
        ) from exc
    workflow.draft_json = patched
    workflow.draft_revision += 1
    await session.commit()
    return {
        "id": workflow.id,
        "draftRevision": workflow.draft_revision,
        "definition": patched,
    }


@router.post("/workflow-versions/{version_id}/workflow-patch", status_code=201)
async def apply_version_workflow_patch(
    version_id: str,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """把受控 WorkflowPatch 应用到某已发布版本，生成**新草稿**（不改旧版本）。

    这是通用 WorkflowPatch 审核闭环：应用总是创建 draft，展示 source→draft diff、
    Compiler diagnostics 与锁变化；人工在编辑器校验并显式发布后才成为新不可变版本。
    ContractPatch 是它的 Browser 子类型，共用同一发布治理。
    """

    version = await session.get(WorkflowVersion, version_id)
    if version is None:
        raise HTTPException(404, "工作流版本不存在")
    try:
        patch = WorkflowPatch.model_validate({**body, "baseVersionId": version_id})
        patched = apply_workflow_patch(version.definition_json, patch)
    except WorkflowPatchViolation as exc:
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_PATCH_REJECTED", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - Pydantic 校验失败也返回 422。
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_PATCH_INVALID", "message": str(exc)},
        ) from exc
    # 用 Compiler 回放校验补丁后的草稿；diagnostics 供人工审核。
    try:
        compiled = compiler.compile(patched)
        diagnostics = [item.model_dump() for item in compiled.diagnostics]
    except WorkflowValidationError as exc:
        diagnostics = [item.model_dump() for item in exc.diagnostics]
    # 应用总是创建新草稿 Workflow，不触碰任何已发布版本。
    draft = Workflow(
        id=str(uuid4()),
        name=f"patch-of-{version.workflow_id[:8]}",
        owner_id="local-user",
        draft_revision=1,
        draft_json=patched,
    )
    session.add(draft)
    await session.commit()
    return {
        "draftWorkflowId": draft.id,
        "baseVersionId": version_id,
        "definition": patched,
        "diagnostics": diagnostics,
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }


@router.get("/components")
async def list_components(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Component).order_by(Component.updated_at.desc()))).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "workflowId": item.workflow_id,
            "defaultVersionId": item.default_version_id,
            "visibility": item.visibility,
            "executionClass": item.execution_class,
        }
        for item in rows
    ]


@router.post("/components", status_code=201)
async def create_component(
    body: ComponentCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    version = await session.get(WorkflowVersion, body.workflow_version_id)
    if version is None:
        raise HTTPException(404, "工作流版本不存在")
    item = await session.scalar(
        select(Component).where(
            Component.workflow_id == version.workflow_id,
            Component.name == body.name,
        )
    )
    if item is None:
        item = Component(
            id=str(uuid4()),
            name=body.name,
            description=body.description,
            workflow_id=version.workflow_id,
            default_version_id=version.id,
            visibility=body.visibility,
            execution_class=version.validation_json.get("executionClass", "zero_llm"),
        )
        session.add(item)
    else:
        item.description = body.description
        item.default_version_id = version.id
        item.visibility = body.visibility
        item.execution_class = version.validation_json.get("executionClass", "zero_llm")
    await session.commit()
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "workflowId": item.workflow_id,
        "defaultVersionId": item.default_version_id,
        "executionClass": item.execution_class,
    }


@router.get("/components/{component_id}")
async def get_component(
    component_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Component, component_id)
    if item is None:
        raise HTTPException(404, "组件不存在")
    version = await session.get(WorkflowVersion, item.default_version_id)
    spec = (version.definition_json.get("spec", {}) if version else {}) or {}
    nodes = spec.get("nodes", [])
    # 业务/技术双视图：副作用节点、确认要求与能力/MCP 依赖。
    side_effect_nodes = [
        {"nodeId": node["id"], "type": node["type"], "displayName": node.get("displayName")}
        for node in nodes
        if node.get("sideEffect")
    ]
    confirmation_nodes = [
        {"nodeId": node["id"], "type": node["type"]}
        for node in nodes
        if node["type"] in {"approval", "browser"} or node.get("sideEffect")
    ]
    validation = version.validation_json if version else {}
    # 最近成功率（最多取该组件最近 20 次运行）。
    recent = (
        await session.scalars(
            select(Run)
            .where(Run.component_id == component_id)
            .order_by(Run.created_at.desc())
            .limit(20)
        )
    ).all()
    terminal = [r for r in recent if r.status in {"succeeded", "failed", "cancelled"}]
    success = [r for r in terminal if r.status == "succeeded"]
    success_rate = round(len(success) / len(terminal), 3) if terminal else None
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "workflowId": item.workflow_id,
        "defaultVersionId": item.default_version_id,
        "visibility": item.visibility,
        "executionClass": item.execution_class,
        "definition": version.definition_json if version else None,
        "businessView": {
            "sideEffectNodes": side_effect_nodes,
            "confirmationNodes": confirmation_nodes,
            "recentSuccessRate": success_rate,
            "recentRunCount": len(terminal),
        },
        "technicalView": {
            "digest": version.digest if version else None,
            "capabilityLocks": validation.get("capabilityLocks", []),
            "mcpLocks": validation.get("mcpLocks", []),
            "nodeCount": len(nodes),
        },
    }


@router.post("/components/{component_id}/runs", status_code=202)
async def create_component_run(
    component_id: str,
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    component = await session.get(Component, component_id)
    if component is None:
        raise HTTPException(404, "组件不存在")
    version = await session.get(WorkflowVersion, component.default_version_id)
    if version is None:
        raise HTTPException(409, "组件默认版本无效")
    try:
        run = await orchestrator.create_run(session, version, body.input, component.id)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "INVALID_INPUT", "message": str(exc)}) from exc
    await session.commit()
    return run_view(run)


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Run).order_by(Run.created_at.desc()).limit(limit))).all()
    return [run_view(item) for item in rows]


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    nodes = (
        await session.scalars(
            select(NodeRun)
            .where(NodeRun.run_id == run_id)
            .order_by(NodeRun.created_at, NodeRun.attempt)
        )
    ).all()
    approvals = (
        await session.scalars(
            select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
        )
    ).all()
    artifacts = (
        await session.scalars(
            select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        )
    ).all()
    checkpoints = (
        await session.scalars(
            select(ExecutionCheckpoint)
            .where(ExecutionCheckpoint.run_id == run_id)
            .order_by(ExecutionCheckpoint.created_at)
        )
    ).all()
    version = await session.get(WorkflowVersion, run.workflow_version_id)
    definition = version.definition_json.get("spec", {}) if version else {}
    result = run_view(
        run,
        list(nodes),
        input_schema=definition.get("inputSchema"),
        output_schema=definition.get("outputSchema"),
    )
    result["approvals"] = [
        {
            "id": item.id,
            "nodeId": item.node_id,
            "status": item.status,
            "snapshotDigest": item.snapshot_digest,
            "snapshot": redact(item.snapshot_json),
        }
        for item in approvals
    ]
    result["artifacts"] = [
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "mediaType": item.media_type,
            "size": item.size,
            "sha256": item.sha256,
            "downloadUrl": f"/api/artifacts/{item.id}",
        }
        for item in artifacts
    ]
    # 从事件流聚合每个节点的可观测指标：Token、Agent steps、MCP 调用数与重试次数。
    events = (
        await session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
        )
    ).all()
    node_metrics: dict[str, dict[str, Any]] = {}
    for event in events:
        node_id = event.payload_json.get("nodeId")
        if node_id is None:
            continue
        bucket = node_metrics.setdefault(node_id, {"retries": 0})
        if event.type == "node.completed":
            # node.completed 的 metrics 已含 Token/agentSteps/mcpCallCount。
            bucket.update(event.payload_json.get("metrics") or {})
        elif event.type == "node.retry_scheduled":
            bucket["retries"] = bucket.get("retries", 0) + 1
    for node in result.get("nodes", []):
        node["metrics"] = node_metrics.get(node["nodeId"], {})
    # 依赖版本：暴露 WorkflowVersion 锁定的 Capability/MCP digest 摘要。
    validation = version.validation_json if version else {}
    result["dependencyVersions"] = {
        "workflowVersionId": run.workflow_version_id,
        "workflowDigest": version.digest if version else None,
        "executionClass": validation.get("executionClass"),
        "capabilityLocks": validation.get("capabilityLocks", []),
        "mcpLocks": validation.get("mcpLocks", []),
    }
    # 检查点/恢复证据：暴露冻结的已完成输出、剩余子图、失败事实与恢复决定，
    # 供运行详情展示“从检查点接力”的可观测视图（脱敏后）。
    result["checkpoints"] = [
        {
            "id": item.id,
            "status": item.status,
            "failedNode": item.failed_node_id,
            "attempt": item.attempt,
            "digest": item.digest,
            "completedNodes": sorted((item.snapshot_json or {}).get("completedOutputs", {})),
            "remainingSubgraph": (item.snapshot_json or {}).get("remainingSubgraph", []),
            "committedSideEffects": (item.snapshot_json or {}).get("committedSideEffects", []),
            "fallbackTarget": (item.snapshot_json or {}).get("failedNode", {}),
            "decision": redact(item.decision_json) if item.decision_json else None,
        }
        for item in checkpoints
    ]
    return result


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    if run.status not in {"succeeded", "failed", "cancelled"}:
        await orchestrator.request_cancellation(session, run.id)
        await session.commit()
    return run_view(run)


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: str,
    body: Decision,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if approval.status != "pending":
        raise HTTPException(409, "审批已处理")
    if approval.snapshot_digest != body.snapshot_digest:
        raise HTTPException(
            409,
            detail={"code": "APPROVAL_SNAPSHOT_CHANGED", "message": "审批快照已变化"},
        )
    if digest_json(approval.snapshot_json) != approval.snapshot_digest:
        raise HTTPException(
            409,
            detail={"code": "APPROVAL_SNAPSHOT_CHANGED", "message": "服务端审批快照已变化"},
        )
    node_run = await session.get(NodeRun, approval.node_run_id)
    if node_run is None or node_run.status != "waiting_user":
        raise HTTPException(409, "关联节点不在等待状态")
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    effect = approval.snapshot_json.get("effect")
    if node_run.node_type == "browser" and effect == "form.submit":
        authorization_digest_value = approval.snapshot_json.get("authorizationDigest")
        if not authorization_digest_value:
            raise HTTPException(409, "浏览器审批缺少服务端授权摘要")
        node_run.status = "ready"
        node_run.input_json = {
            **node_run.input_json,
            "_authorizationDigest": authorization_digest_value,
        }
        node_run.output_json = None
        node_run.error_json = None
        node_run.available_at = utcnow()
    elif node_run.node_type == "browser" and effect == "browser.drift":
        drift_authorization_value = approval.snapshot_json.get("driftAuthorization")
        if not drift_authorization_value:
            raise HTTPException(409, "浏览器审批缺少服务端漂移授权摘要")
        node_run.status = "ready"
        node_run.input_json = {
            **node_run.input_json,
            "_driftAuthorization": drift_authorization_value,
        }
        node_run.output_json = None
        node_run.error_json = None
        node_run.available_at = utcnow()
    elif (
        node_run.node_type == "browser"
        and (node_run.error_json or {}).get("code") == "UNKNOWN_OUTCOME"
    ):
        node_run.status = "failed"
        node_run.finished_at = utcnow()
    else:
        node_run.status = "succeeded"
        node_run.output_json = {
            "approved": True,
            "snapshot": approval.snapshot_json.get("payload", {}),
        }
        node_run.finished_at = utcnow()
    await append_event(
        session,
        approval.run_id,
        "approval.resolved",
        {"approvalId": approval.id, "nodeId": approval.node_id, "status": "approved"},
        node_run.id,
    )
    await orchestrator.advance_run(session, approval.run_id)
    await session.commit()
    return {"id": approval.id, "status": approval.status}


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if approval.status != "pending":
        raise HTTPException(409, "审批已处理")
    node_run = await session.get(NodeRun, approval.node_run_id)
    approval.status = "rejected"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    if node_run:
        node_run.status = "failed"
        node_run.error_json = {
            "code": "CONFIRMATION_DENIED",
            "message": "用户拒绝审批",
            "retryable": False,
        }
        node_run.finished_at = utcnow()
    await append_event(
        session,
        approval.run_id,
        "approval.resolved",
        {"approvalId": approval.id, "nodeId": approval.node_id, "status": "rejected"},
        approval.node_run_id,
    )
    await orchestrator.advance_run(session, approval.run_id)
    await session.commit()
    return {"id": approval.id, "status": approval.status}


@router.get("/runs/{run_id}/events")
async def list_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    events = (
        await session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after)
            .order_by(RunEvent.seq)
        )
    ).all()
    return [
        {
            "id": item.id,
            "seq": item.seq,
            "type": item.type,
            "payload": item.payload_json,
            "createdAt": item.created_at,
        }
        for item in events
    ]


@router.get("/runs/{run_id}/events/stream")
async def stream_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    async def generate() -> Any:
        cursor = int(last_event_id or 0)
        while not await request.is_disconnected():
            async for session in get_session():
                events = (
                    await session.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.seq > cursor)
                        .order_by(RunEvent.seq)
                    )
                ).all()
                run = await session.get(Run, run_id)
            for item in events:
                cursor = item.seq
                data = {
                    "runId": run_id,
                    "seq": item.seq,
                    "payload": item.payload_json,
                }
                yield f"id: {item.seq}\nevent: {item.type}\ndata: {digest_safe_json(data)}\n\n"
            if run and run.status in {"succeeded", "failed", "cancelled"} and not events:
                break
            if not events:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")


def digest_safe_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


@router.get("/artifacts/{artifact_id}")
async def download_artifact(
    artifact_id: str, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(404, "Artifact 不存在")
    root = get_settings().artifact_root.resolve()
    storage_key = artifact.metadata_json.get("storageKey")
    if not storage_key:
        raise HTTPException(410, "Artifact 存储键不可用")
    path = (root / artifact.run_id / storage_key).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(410, "Artifact 文件不可用")
    if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256:
        raise HTTPException(409, "Artifact 完整性校验失败")
    return FileResponse(path, media_type=artifact.media_type, filename=artifact.name)


@router.get("/capabilities")
async def list_capabilities(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Capability).order_by(Capability.name))).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "type": item.type,
            "visibility": item.visibility,
            "currentVersionId": item.current_version_id,
        }
        for item in rows
    ]


@router.get("/secrets")
async def list_secret_references(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    references = (
        await session.scalars(select(SecretReference).order_by(SecretReference.name))
    ).all()
    return [
        {
            "id": item.id,
            "name": item.name,
            "provider": item.provider,
            "environmentVariable": item.external_key,
            "description": item.description,
        }
        for item in references
    ]


@router.post("/secrets", status_code=201)
async def create_secret_reference(
    body: SecretReferenceCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        reference = await register_secret_reference(
            session,
            body.name,
            body.environment_variable,
            body.description,
        )
        await session.commit()
    except SecretResolutionError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "INVALID_SECRET_REFERENCE", "message": str(exc)},
        ) from exc
    return {
        "id": reference.id,
        "name": reference.name,
        "provider": reference.provider,
        "environmentVariable": reference.external_key,
        "description": reference.description,
    }


@router.post("/mcp-servers", status_code=201)
async def create_mcp_server(
    body: MCPServerCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        server, version = await register_server(
            session,
            body.name,
            body.transport,
            body.config,
        )
        await session.commit()
    except MCPConfigurationError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "INVALID_MCP_SERVER", "message": str(exc)},
        ) from exc
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "currentVersionId": version.id,
        "digest": version.digest,
    }


@router.post("/mcp-servers/{server_id}/discover")
async def discover_mcp_server(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    server = await session.get(MCPServer, server_id)
    if server is None or not server.current_version_id:
        raise HTTPException(404, "MCP Server 不存在")
    try:
        snapshots = await discover_tools(session, server.current_version_id)
        await session.commit()
    except MCPContractDriftError as exc:
        await session.rollback()
        raise HTTPException(
            409,
            detail={"code": "MCP_CONTRACT_DRIFT", "message": str(exc)},
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            502,
            detail={"code": "MCP_DISCOVERY_FAILED", "message": str(exc)},
        ) from exc
    return [
        {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "inputSchema": item.input_schema_json,
            "outputSchema": item.output_schema_json,
            "digest": item.digest,
            "effect": item.side_effect,
            "idempotent": item.idempotent,
            "effectSource": item.effect_source,
            "effectReviewStatus": item.effect_review_status,
        }
        for item in snapshots
    ]


@router.get("/mcp-servers/{server_id}/dependents")
async def scan_mcp_dependents(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """扫描依赖该 Server 早期 Tool 契约的 WorkflowVersion，标出兼容/破坏性变化。"""

    server = await session.get(MCPServer, server_id)
    if server is None or not server.current_version_id:
        raise HTTPException(404, "MCP Server 不存在")
    return await scan_dependent_workflows(session, server_id, server.current_version_id)


@router.post("/mcp-servers/{server_id}/health")
async def health_mcp_server(
    server_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await session.get(MCPServer, server_id)
    if server is None or not server.current_version_id:
        raise HTTPException(404, "MCP Server 不存在")
    try:
        snapshots = await discover_tools(session, server.current_version_id)
    except Exception as exc:
        raise HTTPException(
            503,
            detail={"code": "MCP_UNAVAILABLE", "message": str(exc)},
        ) from exc
    return {"status": "healthy", "toolCount": len(snapshots)}


@router.post("/mcp-tools/{snapshot_id}/classify")
async def classify_mcp_tool(
    snapshot_id: str,
    body: MCPToolClassify,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """人工审核并覆盖某 ToolSnapshot 的 effect/idempotent 分类。"""

    try:
        snapshot = await classify_snapshot(
            session,
            snapshot_id,
            effect=body.effect,
            idempotent=body.idempotent,
        )
        await session.commit()
    except MCPConfigurationError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "INVALID_MCP_CLASSIFICATION", "message": str(exc)},
        ) from exc
    return {
        "id": snapshot.id,
        "name": snapshot.name,
        "effect": snapshot.side_effect,
        "idempotent": snapshot.idempotent,
        "effectSource": snapshot.effect_source,
        "effectReviewStatus": snapshot.effect_review_status,
        "digest": snapshot.digest,
    }


@router.post("/mcp-tools/{snapshot_id}/test")
async def test_mcp_tool(
    snapshot_id: str,
    body: MCPToolTest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        outcome = await call_snapshot_through_contract(session, snapshot_id, body.arguments)
    except MCPConfigurationError as exc:
        raise HTTPException(
            404,
            detail={"code": "MCP_SNAPSHOT_UNAVAILABLE", "message": str(exc)},
        ) from exc
    snapshot = await session.get(MCPToolSnapshot, snapshot_id)
    effect_view = (
        {
            "effect": snapshot.side_effect,
            "idempotent": snapshot.idempotent,
            # 与 Orchestrator 共用唯一重试策略，避免测试页与运行时语义不一致。
            "autoRetryAllowed": auto_retry_allowed(snapshot.side_effect, snapshot.idempotent),
            "effectReviewStatus": snapshot.effect_review_status,
        }
        if snapshot is not None
        else {}
    )
    if outcome.status == "succeeded":
        return {
            "status": "succeeded",
            "output": outcome.output,
            "toolName": outcome.tool_name,
            "toolDigest": outcome.tool_digest,
            **effect_view,
        }
    # 与 Workflow MCPExecutor 一致的结构化失败结果，而非裸异常。
    raise HTTPException(
        502,
        detail={
            "code": outcome.error_code or "MCP_CALL_FAILED",
            "message": outcome.error_message or "MCP 调用失败",
            "retryable": outcome.retryable,
            **effect_view,
        },
    )


@router.post("/capabilities", status_code=201)
async def register_capability(
    body: CapabilityCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if body.type != "skillx" or not body.package_path:
        raise HTTPException(422, "Local Demo 当前仅支持注册 SkillX 相对包路径")
    try:
        capability, version = await register_skillx(session, body.package_path)
        if body.name != capability.name:
            raise ValueError("请求名称必须与 Manifest metadata.name 一致")
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(422, detail={"code": "INVALID_SKILLX", "message": str(exc)}) from exc
    return {
        "id": capability.id,
        "name": capability.name,
        "type": capability.type,
        "currentVersionId": version.id,
        "digest": version.digest,
    }


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = Conversation(
        id=str(uuid4()),
        title=body.title,
        bound_component_id=body.bound_component_id,
    )
    session.add(item)
    await session.commit()
    return {
        "id": item.id,
        "title": item.title,
        "boundComponentId": item.bound_component_id,
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Conversation, conversation_id)
    if item is None:
        raise HTTPException(404, "对话不存在")
    messages = (
        await session.scalars(
            select(Message).where(Message.conversation_id == item.id).order_by(Message.created_at)
        )
    ).all()
    return {
        "id": item.id,
        "title": item.title,
        "boundComponentId": item.bound_component_id,
        "messages": [
            {"id": message.id, "role": message.role, "content": message.content_json}
            for message in messages
        ],
    }


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def create_message(
    conversation_id: str,
    body: MessageCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    user_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="user",
        content_json={"type": "text", "text": body.content},
    )
    component_id = body.component_id or conversation.bound_component_id
    assistant_content: dict[str, Any]
    conversation_run: ConversationRun | None = None
    if component_id:
        # 绑定组件或显式指定组件：补参齐全则启动 Workflow，否则请求补参。
        assistant_content = await _bound_component_message(
            session, component_id, body.content, body.inputs
        )
    else:
        candidate = await match_component(session, body.content)
        if candidate is not None:
            assistant_content = candidate_message(candidate)
        else:
            assistant_content, conversation_run = await _general_agent_message(
                conversation, body.content
            )
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content_json=assistant_content,
    )
    session.add_all(
        [
            user_message,
            assistant_message,
            *([conversation_run] if conversation_run is not None else []),
        ]
    )
    await session.commit()
    return {
        "userMessageId": user_message.id,
        "assistant": assistant_message.content_json,
    }


async def _bound_component_message(
    session: AsyncSession,
    component_id: str,
    content: str,
    inputs: dict[str, Any] | None,
) -> dict[str, Any]:
    component = await session.get(Component, component_id)
    version = (
        await session.get(WorkflowVersion, component.default_version_id) if component else None
    )
    if component is None or version is None:
        raise HTTPException(404, "组件或默认版本不存在")
    spec = version.definition_json["spec"]
    schema = spec["inputSchema"]
    required = schema.get("required", [])
    if inputs is None and len(required) == 1:
        inputs = {required[0]: content}
    missing = [field for field in required if not inputs or field not in inputs]
    if missing:
        candidate = ComponentCandidate(
            component_id=component.id,
            component_name=component.name,
            workflow_version_id=version.id,
            input_schema=schema,
            ui_schema=spec.get("uiSchema", {}),
            execution_class=component.execution_class,
            confidence=1.0,
            reason="已绑定组件",
        )
        return candidate_message(candidate, missing=missing)
    try:
        run = await orchestrator.create_run(session, version, inputs or {}, component.id)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "INVALID_INPUT", "message": str(exc)},
        ) from exc
    return {
        "type": "component_run",
        "state": "component_run",
        "componentId": component.id,
        "componentName": component.name,
        "workflowVersionId": version.id,
        "runId": run.id,
        "text": "组件已启动，后续执行由 Workflow Runtime 接管。",
    }


async def _general_agent_message(
    conversation: Conversation,
    content: str,
) -> tuple[dict[str, Any], ConversationRun | None]:
    """未匹配组件的通用任务。

    区分运行类型：单次模型回答记为 ``llm_response``，绝不伪装成 ``agent_run``。
    真实 Agent 运行事实只能由服务端 AgentRunService 通过
    ``POST /conversations/{id}/agent-runs`` 创建。此处始终附带 ``offerAgentRun`` 提示，
    前端据此提供“启动通用 Agent”入口。
    """

    settings = get_settings()
    if not settings.model_endpoint or not settings.model_name:
        return (
            {
                "type": "error",
                "code": "MODEL_NOT_CONFIGURED",
                "text": "未匹配到组件，且通用 LLM/Agent Runtime 尚未配置。",
                "offerAgentRun": bool(settings.traex_command),
            },
            None,
        )
    output_schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    try:
        model_result = await OpenAICompatibleAdapter().generate(
            "请直接回答用户请求，并输出结构化 reply 字段。\n用户：" + content,
            output_schema,
            1200,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            detail={"code": "MODEL_CALL_FAILED", "message": str(exc)},
        ) from exc
    conversation_run = ConversationRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        status="succeeded",
        run_type="llm_response",
        input_json={"message": content},
        output_json=model_result.output,
        trajectory_json={
            "runType": "llm_response",
            "name": "conversation-draft",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "outputSchema": output_schema,
            "steps": [
                {
                    "id": "answer",
                    "kind": "llm",
                    "displayName": "生成回答",
                    "input": {"message": "${input.message}"},
                    "config": {
                        "prompt": "请回答输入消息：{input}",
                        "outputSchema": output_schema,
                        "maxOutputTokens": 1200,
                    },
                }
            ],
            "outputMapping": {"reply": "${answer.output.reply}"},
            "originalModelCalls": 1,
        },
    )
    assistant_content = {
        "type": "llm_response",
        "state": "llm_response",
        "text": model_result.output["reply"],
        "conversationRunId": conversation_run.id,
        "runType": "llm_response",
        # 单次回答不是可固化的 Agent 运行；提示前端可升级为真实通用 Agent。
        "offerAgentRun": bool(get_settings().traex_command),
    }
    return assistant_content, conversation_run


@router.post("/conversations/{conversation_id}/confirm-component", status_code=201)
async def confirm_component(
    conversation_id: str,
    body: ComponentConfirm,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """用户在候选卡片上确认并补参后显式启动组件。

    服务端重新计算候选快照摘要，确认组件版本与 inputSchema 自展示以来未变化，再校验补参
    并启动 Workflow。该摘要仅用于检测 stale，不是授权或一次性令牌。
    """

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    component = await session.get(Component, body.component_id)
    version = (
        await session.get(WorkflowVersion, component.default_version_id) if component else None
    )
    if component is None or version is None:
        raise HTTPException(404, "组件或默认版本不存在")
    spec = version.definition_json["spec"]
    schema = spec["inputSchema"]
    expected_digest = candidate_snapshot_digest(component.id, version.id, schema)
    if expected_digest != body.candidate_snapshot_digest:
        raise HTTPException(
            409,
            detail={
                "code": "CANDIDATE_STALE",
                "message": "组件默认版本或输入契约已变化，请重新获取候选。",
            },
        )
    missing = [field for field in schema.get("required", []) if field not in body.inputs]
    if missing:
        raise HTTPException(
            422,
            detail={"code": "MISSING_INPUTS", "message": "缺少必填参数", "fields": missing},
        )
    try:
        run = await orchestrator.create_run(session, version, body.inputs, component.id)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "INVALID_INPUT", "message": str(exc)},
        ) from exc
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content_json={
            "type": "component_run",
            "state": "component_run",
            "componentId": component.id,
            "componentName": component.name,
            "workflowVersionId": version.id,
            "runId": run.id,
            "text": "组件已启动，后续执行由 Workflow Runtime 接管。",
        },
    )
    session.add(assistant_message)
    await session.commit()
    return {"runId": run.id, "assistant": assistant_message.content_json}


@router.post("/conversations/{conversation_id}/agent-runs", status_code=202)
async def start_agent_run(
    conversation_id: str,
    body: AgentRunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """从通用对话启动一次受限 TraeX Agent 探索（先持久化，后执行）。

    只在短事务内创建 ``queued`` ConversationRun、``agent.run.created`` 事件与关联的
    assistant message，随即返回 202 + handle。真正的 TraeX 执行由独立 Agent Worker
    领取租约后完成，运行中事件逐批提交、可跨连接/进程恢复。请求方提交的
    ``allowedCapabilities`` 只能收窄，实际权限为它与 Host policy 的交集。
    """

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(404, "对话不存在")
    default_config = AgentRunConfig.default()
    config = AgentRunConfig(
        output_schema=default_config.output_schema,
        max_steps=min(body.max_steps or default_config.max_steps, default_config.max_steps),
        max_total_tokens=default_config.max_total_tokens,
        allowed_capabilities=tuple(
            body.allowed_capabilities
            if body.allowed_capabilities is not None
            else default_config.allowed_capabilities
        ),
        max_duration_seconds=default_config.max_duration_seconds,
    )
    conversation_run = await enqueue_agent_run(
        session,
        conversation,
        body.content,
        config=config,
    )
    assistant_content = {
        "type": "agent_run",
        "state": "agent_run",
        "text": "通用 Agent 已进入队列，运行事件将实时更新。",
        "conversationRunId": conversation_run.id,
        "runType": "agent_run",
    }
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content_json=assistant_content,
    )
    session.add(assistant_message)
    await session.commit()
    return {
        "conversationRunId": conversation_run.id,
        "status": conversation_run.status,
        "assistant": assistant_content,
    }


@router.post("/conversation-runs/{conversation_run_id}/cancel", status_code=202)
async def cancel_conversation_run(
    conversation_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """请求取消一次 Agent 运行。

    queued 直接置为 cancelled；running 记录取消请求，由持有租约的 Worker 传播到子进程
    并最终写入 ``cancelled`` 终态。
    """

    try:
        run = await request_cancellation(session, conversation_run_id)
    except LookupError as exc:
        raise HTTPException(404, "ConversationRun 不存在") from exc
    await session.commit()
    return {"conversationRunId": run.id, "status": run.status}


@router.get("/conversation-runs/{conversation_run_id}")
async def get_conversation_run(
    conversation_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conversation_run = await session.get(ConversationRun, conversation_run_id)
    if conversation_run is None:
        raise HTTPException(404, "ConversationRun 不存在")
    return summarize_run(conversation_run)


@router.get("/conversation-runs/{conversation_run_id}/events")
async def list_conversation_run_events(
    conversation_run_id: str,
    after: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if await session.get(ConversationRun, conversation_run_id) is None:
        raise HTTPException(404, "ConversationRun 不存在")
    events = (
        await session.scalars(
            select(ConversationRunEvent)
            .where(
                ConversationRunEvent.conversation_run_id == conversation_run_id,
                ConversationRunEvent.seq > after,
            )
            .order_by(ConversationRunEvent.seq)
        )
    ).all()
    return [
        {
            "seq": item.seq,
            "type": item.type,
            "payload": item.payload_json,
            "createdAt": item.created_at,
        }
        for item in events
    ]


@router.post("/conversations/{conversation_id}/trajectory-imports", status_code=201)
async def import_conversation_trajectory(
    conversation_id: str,
    body: ConversationRunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """导入外部轨迹。

    标记为 ``trajectory_import``，不得作为真实成功 Agent 运行事实。``agent_run`` 只能
    由服务端 AgentRunService 创建，见 ``POST /conversations/{id}/agent-runs``。
    """

    if await session.get(Conversation, conversation_id) is None:
        raise HTTPException(404, "对话不存在")
    run = ConversationRun(
        id=str(uuid4()),
        conversation_id=conversation_id,
        status="succeeded",
        run_type="trajectory_import",
        input_json=body.input,
        output_json=body.output,
        trajectory_json={**body.trajectory, "runType": "trajectory_import"},
    )
    session.add(run)
    await session.commit()
    return {"id": run.id, "status": run.status, "runType": run.run_type}


@router.post("/conversation-runs/{conversation_run_id}/distill", status_code=201)
async def distill_run(
    conversation_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    conversation_run = await session.get(ConversationRun, conversation_run_id)
    if conversation_run is None:
        raise HTTPException(404, "ConversationRun 不存在")
    try:
        result = await distill_conversation_run(session, conversation_run)
        await session.commit()
    except DistillationError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "DISTILLATION_FAILED", "message": str(exc)},
        ) from exc
    return {
        "id": result.id,
        "status": result.status,
        "draftWorkflowId": result.draft_workflow_id,
        "analysis": result.analysis_json,
    }


@router.get("/distillations/{distillation_id}")
async def get_distillation(
    distillation_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    result = await session.get(Distillation, distillation_id)
    if result is None:
        raise HTTPException(404, "Distillation 不存在")
    return {
        "id": result.id,
        "status": result.status,
        "draftWorkflowId": result.draft_workflow_id,
        "analysis": result.analysis_json,
    }
