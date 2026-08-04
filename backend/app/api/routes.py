from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
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
from app.application.plans import (
    PlanCompileError,
    WorkflowSelection,
    create_plan_snapshot,
    expire_plan_snapshot,
    verify_plan_snapshot,
)
from app.application.replay import run_replay_proof
from app.application.retries import auto_retry_allowed
from app.application.secrets import (
    SecretResolutionError,
    register_secret_reference,
)
from app.application.security import redact
from app.application.workflow_catalog import (
    CatalogError,
    build_manifest,
    consume_session_call,
    create_disclosure_session,
    create_private_fragment_snapshot,
    describe_fragment,
    describe_workflow,
    disclose_refs,
    record_disclosure_response,
    require_disclosed_ref,
    require_plan_refs_disclosed,
    resolve_workflow_manifest,
    search_manifests,
)
from app.application.workflow_patch import (
    WorkflowPatch,
    WorkflowPatchViolation,
    apply_workflow_patch,
    observation_eligibility,
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
    FailureObservation,
    MCPServer,
    MCPToolSnapshot,
    Message,
    NodeRun,
    PlanSnapshot,
    ReplayEvidence,
    Run,
    RunEvent,
    SecretReference,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowDisclosureSession,
    WorkflowFragmentManifest,
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


class RecoveryAnswer(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")
    answer: Any


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


class WorkflowSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=5, ge=1, le=8)
    session_id: str = Field(alias="sessionId", min_length=1)


class WorkflowDescribeRequest(BaseModel):
    ref: str = Field(min_length=1, max_length=300)
    session_id: str = Field(alias="sessionId", min_length=1)


class WorkflowSliceRequest(BaseModel):
    workflow_ref: str = Field(alias="workflowRef", min_length=1, max_length=300)
    candidate_node_ids: list[str] = Field(alias="candidateNodeIds", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)


class PlanCreate(BaseModel):
    plan: dict[str, Any]
    created_by_conversation_run_id: str | None = Field(
        default=None, alias="createdByConversationRunId"
    )
    ttl_hours: int = Field(default=168, alias="ttlHours", ge=1, le=8760)
    session_id: str = Field(alias="sessionId", min_length=1)


class DisclosureSessionCreate(BaseModel):
    conversation_id: str | None = Field(default=None, alias="conversationId")
    agent_run_id: str | None = Field(default=None, alias="agentRunId")
    ttl_minutes: int = Field(default=60, alias="ttlMinutes", ge=1, le=1440)


class PlanRunCreate(BaseModel):
    input: dict[str, Any]


class PlanSolidifyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


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
        "planSnapshotId": run.plan_snapshot_id,
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
    await session.flush()
    try:
        manifest = await build_manifest(session, item, version)
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "CAPABILITY_MANIFEST_INVALID", "message": str(exc)},
        ) from exc
    await session.commit()
    await session.refresh(version)
    return {
        "id": version.id,
        "workflowId": version.workflow_id,
        "version": version.version,
        "digest": version.digest,
        "definition": version.definition_json,
        "workflowRef": manifest.manifest_json["workflowRef"],
        "manifestDigest": manifest.digest,
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
    """把受控 WorkflowPatch 应用到某已发布版本，真实 replay 通过后生成**新草稿**。

    治理闭环（006-D）：
    1. 必须提供 ``sourceRunId``（或 ``failureFingerprint``）指向真实失败；资格由后端
       FailureObservation 事实计算，不接受前端自报 reproducible；
    2. 补丁不得扩权（能力锁/权限/模型/Secret 由 DSL 白名单封死）；
    3. 用 source run 的安全输入在隔离环境**真实 replay** patched 图到原失败点；
    4. replay 通过后才创建 draft，旧版本不变，仍需人工发布。
    """

    version = await session.get(WorkflowVersion, version_id)
    if version is None:
        raise HTTPException(404, "工作流版本不存在")

    # 1) 资格：从后端 FailureObservation 计算，前端不能自报。
    source_run_id = body.get("sourceRunId")
    fingerprint = body.get("failureFingerprint")
    observation = None
    if fingerprint:
        observation = await session.scalar(
            select(FailureObservation).where(FailureObservation.fingerprint == fingerprint)
        )
    elif source_run_id:
        observation = await session.scalar(
            select(FailureObservation)
            .where(FailureObservation.last_run_id == source_run_id)
            .order_by(FailureObservation.updated_at.desc())
        )
    if observation is None:
        raise HTTPException(
            422,
            detail={
                "code": "PATCH_NOT_ELIGIBLE",
                "message": "缺少可归因的失败观测；补丁必须由真实可复现失败驱动",
            },
        )
    if observation.workflow_version_id != version_id:
        raise HTTPException(
            422,
            detail={
                "code": "PATCH_SOURCE_MISMATCH",
                "message": "失败观测与目标 WorkflowVersion 不一致",
            },
        )
    eligibility = observation_eligibility(observation)

    # 2) 应用受控补丁（扩权/敏感字段被 DSL 拒绝）。
    patch_body = body.get("patch", body)
    try:
        patch = WorkflowPatch.model_validate({**patch_body, "baseVersionId": version_id})
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

    # 3) 真实 replay：用 source run 的安全输入在隔离环境跑到原失败点。
    if not source_run_id or source_run_id != observation.last_run_id:
        raise HTTPException(
            422,
            detail={
                "code": "PATCH_SOURCE_MISMATCH",
                "message": "必须使用 observation 绑定的 sourceRunId",
            },
        )
    source_run = await session.get(Run, source_run_id)
    if source_run is None or source_run.workflow_version_id != version_id:
        raise HTTPException(
            422,
            detail={
                "code": "PATCH_SOURCE_MISMATCH",
                "message": "source Run 与目标 WorkflowVersion 不一致",
            },
        )
    replay = await run_replay_proof(
        version.definition_json,
        patched,
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        expected_error_code=observation.error_code,
    )
    observation.baseline_reproduced = replay["baselineReproduced"]
    eligibility = observation_eligibility(observation)
    evidence = ReplayEvidence(
        id=str(uuid4()),
        observation_id=observation.id,
        baseline_snapshot_json=replay["baseline"],
        patched_snapshot_json=replay["patched"] or {},
        diff_json=replay["diff"],
        passed=replay["passed"],
    )
    session.add(evidence)
    if not eligibility["eligible"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": "PATCH_NOT_ELIGIBLE",
                "message": eligibility["reason"],
                **eligibility,
                "replayEvidenceId": evidence.id,
            },
        )
    if not replay["passed"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": "REPLAY_FAILED",
                "message": (
                    "真实 replay 未通过（原失败点状态="
                    f"{replay['baseline']['failedNodeStatus']}），不生成草稿"
                ),
                "replay": replay,
            },
        )

    # 4) replay 通过：创建新草稿（不触碰已发布版本）。
    try:
        compiled = compiler.compile(patched)
        diagnostics = [item.model_dump() for item in compiled.diagnostics]
    except WorkflowValidationError as exc:
        diagnostics = [item.model_dump() for item in exc.diagnostics]
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
        "eligibility": eligibility,
        "replay": {
            "replayEvidenceId": evidence.id,
            "passed": replay["passed"],
            "baselineReproduced": replay["baselineReproduced"],
            "failedNodeStatus": replay["patched"]["failedNodeStatus"],
            "sideEffectsSimulated": replay["patched"]["sideEffectsSimulated"],
        },
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }


@router.post("/plans/{plan_id}/plan-patch", status_code=201)
async def apply_plan_snapshot_patch(
    plan_id: str,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Replay a controlled patch against a Plan failure and create a new snapshot."""

    snapshot = await session.get(PlanSnapshot, plan_id)
    source_run_id = body.get("sourceRunId")
    observation = await session.scalar(
        select(FailureObservation)
        .where(
            FailureObservation.last_run_id == source_run_id,
            FailureObservation.plan_snapshot_id == plan_id,
        )
        .order_by(FailureObservation.updated_at.desc())
    )
    source_run = await session.get(Run, source_run_id) if source_run_id else None
    if snapshot is None or observation is None or source_run is None:
        raise HTTPException(
            422,
            detail={"code": "PATCH_SOURCE_MISMATCH", "message": "Plan 失败源不匹配"},
        )
    try:
        patch = WorkflowPatch.model_validate(
            {**body.get("patch", body), "baseVersionId": plan_id}
        )
        patched = apply_workflow_patch(snapshot.definition_json, patch)
        compiled = compiler.compile(patched)
    except (WorkflowPatchViolation, WorkflowValidationError, ValueError) as exc:
        raise HTTPException(
            422,
            detail={"code": "PLAN_PATCH_REJECTED", "message": str(exc)},
        ) from exc
    replay = await run_replay_proof(
        snapshot.definition_json,
        compiled.definition.model_dump(by_alias=True, mode="json"),
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        expected_error_code=observation.error_code,
    )
    observation.baseline_reproduced = replay["baselineReproduced"]
    evidence = ReplayEvidence(
        id=str(uuid4()),
        observation_id=observation.id,
        baseline_snapshot_json=replay["baseline"],
        patched_snapshot_json=replay["patched"] or {},
        diff_json=replay["diff"],
        passed=replay["passed"],
    )
    session.add(evidence)
    eligibility = observation_eligibility(observation)
    if not replay["passed"] or not eligibility["eligible"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": "REPLAY_FAILED",
                "message": eligibility["reason"],
                "replayEvidenceId": evidence.id,
            },
        )
    definition = compiled.definition.model_dump(by_alias=True, mode="json")
    payload = {
        "definition": definition,
        "sourceRefs": snapshot.source_refs_json,
        "locks": snapshot.locks_json,
        "sourceMap": snapshot.source_map_json,
    }
    replacement = PlanSnapshot(
        id=str(uuid4()),
        definition_json=definition,
        digest=digest_json(payload),
        source_refs_json=snapshot.source_refs_json,
        locks_json=snapshot.locks_json,
        source_map_json=snapshot.source_map_json,
        created_by_conversation_run_id=snapshot.created_by_conversation_run_id,
        expires_at=utcnow() + timedelta(hours=168),
        status="active",
        plan_signature=snapshot.plan_signature,
    )
    session.add(replacement)
    await session.commit()
    return {
        "planSnapshotId": replacement.id,
        "sourcePlanSnapshotId": snapshot.id,
        "replayEvidenceId": evidence.id,
        "published": False,
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


@router.post("/workflow-tools/sessions", status_code=201)
async def create_workflow_disclosure_session(
    body: DisclosureSessionCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await create_disclosure_session(
        session,
        conversation_id=body.conversation_id,
        agent_run_id=body.agent_run_id,
        ttl_minutes=body.ttl_minutes,
    )
    await session.commit()
    return {
        "id": item.id,
        "status": item.status,
        "expiresAt": item.expires_at,
        "callBudget": item.call_budget_json,
    }


@router.post("/workflow-tools/search")
async def workflow_search(
    body: WorkflowSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """L0：有界 top-k 能力卡片，不返回完整 Schema 或 DAG。"""

    try:
        await consume_session_call(
            session,
            body.session_id,
            "search",
            {"queryDigest": digest_json(body.query), "limit": body.limit},
        )
        items = await search_manifests(
            session, body.query, constraints=body.constraints, limit=body.limit
        )
        await disclose_refs(
            session,
            body.session_id,
            [str(item["workflowRef"]) for item in items],
            "L0",
        )
        response = {"level": "L0", "count": len(items), "items": items}
        await record_disclosure_response(
            session, body.session_id, "search", response, level="L0"
        )
        await session.commit()
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            429, detail={"code": "WORKFLOW_TOOL_LIMIT", "message": str(exc)}
        ) from exc
    return response


@router.post("/workflow-tools/describe")
async def workflow_describe(
    body: WorkflowDescribeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """L1：按 Registry 返回的精确 workflowRef 披露契约，不返回技术 DAG。"""

    try:
        await consume_session_call(
            session, body.session_id, "describe", {"refDigest": digest_json(body.ref)}
        )
        await require_disclosed_ref(session, body.session_id, body.ref)
        result = await describe_workflow(session, body.ref)
        await disclose_refs(
            session,
            body.session_id,
            [
                item["fragmentRef"]
                for item in result.get("fragments", [])
                if item.get("fragmentRef")
            ],
            "L1",
        )
        response = {"level": "L1", "workflow": result}
        await record_disclosure_response(
            session, body.session_id, "describe", response, level="L1"
        )
        await session.commit()
        return response
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            404,
            detail={"code": "WORKFLOW_REF_NOT_FOUND", "message": str(exc)},
        ) from exc


@router.post("/workflow-tools/describe-fragment")
async def workflow_describe_fragment(
    body: WorkflowDescribeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """L2：仅按已选择的精确 fragmentRef 披露边界与锁。"""

    try:
        await consume_session_call(
            session, body.session_id, "describe", {"refDigest": digest_json(body.ref)}
        )
        await require_disclosed_ref(session, body.session_id, body.ref)
        response = {
            "level": "L2",
            "fragment": await describe_fragment(session, body.ref),
        }
        await disclose_refs(session, body.session_id, [body.ref], "L2")
        await record_disclosure_response(
            session, body.session_id, "describe", response, level="L2"
        )
        await session.commit()
        return response
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            404,
            detail={"code": "FRAGMENT_REF_NOT_FOUND", "message": str(exc)},
        ) from exc


@router.post("/workflow-tools/slice", status_code=201)
async def workflow_slice(
    body: WorkflowSliceRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await consume_session_call(
            session,
            body.session_id,
            "slice",
            {
                "workflowRefDigest": digest_json(body.workflow_ref),
                "candidateNodeIds": sorted(body.candidate_node_ids),
            },
        )
        await require_disclosed_ref(session, body.session_id, body.workflow_ref)
        snapshot = await create_private_fragment_snapshot(
            session, body.workflow_ref, body.candidate_node_ids
        )
        await disclose_refs(
            session, body.session_id, [snapshot.fragment_ref], "L2"
        )
        response = {
            "level": "L2",
            "fragment": {**snapshot.manifest_json, "manifestDigest": snapshot.digest},
        }
        await record_disclosure_response(
            session, body.session_id, "slice", response, level="L2"
        )
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            422, detail={"code": "FRAGMENT_SLICE_REJECTED", "message": str(exc)}
        ) from exc
    await session.commit()
    return response


@router.post("/workflow-tools/validate-selection")
async def validate_workflow_selection(
    body: WorkflowSelection,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """校验 Agent 的结构化选择；ref 必须能由已发布 Registry 精确解析。"""

    try:
        for ref in body.refs:
            await require_disclosed_ref(session, body.session_id, ref)
            if ref.startswith("workflow:"):
                await describe_workflow(session, ref)
            elif ref.startswith(("fragment:", "fragment-snapshot:")):
                await describe_fragment(session, ref)
            else:
                raise CatalogError("选择包含 Registry 未返回的 ref 类型")
        if body.action == "compose" and body.plan is not None:
            await require_plan_refs_disclosed(
                session, body.session_id, body.plan
            )
    except CatalogError as exc:
        raise HTTPException(
            422, detail={"code": "WORKFLOW_SELECTION_REJECTED", "message": str(exc)}
        ) from exc
    return {"valid": True, "selection": body.model_dump(by_alias=True)}


@router.post("/workflow-tools/execute-selection", status_code=202)
async def execute_workflow_selection(
    body: WorkflowSelection,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Execute each Selection branch without silently coercing it to run_full."""

    if body.action == "ask_user":
        return {
            "action": body.action,
            "status": "awaiting_input",
            "question": body.question,
            "missingInputs": body.missing_inputs,
            "sessionId": body.session_id,
        }
    if body.action == "continue_agent":
        return {
            "action": body.action,
            "status": "continue",
            "reason": body.reason,
            "sessionId": body.session_id,
        }
    try:
        for ref in body.refs:
            await require_disclosed_ref(session, body.session_id, ref)
        if body.action == "run_full":
            manifest = await resolve_workflow_manifest(session, body.refs[0])
            version = await session.get(WorkflowVersion, manifest.workflow_version_id)
            if version is None:
                raise CatalogError("WorkflowVersion 不存在")
            run = await orchestrator.create_run(session, version, body.bindings)
            await session.commit()
            return {
                "action": body.action,
                "status": run.status,
                "runId": run.id,
                "workflowVersionId": version.id,
            }
        if body.action == "run_fragment":
            fragment = await describe_fragment(session, body.refs[0])
            input_schema = fragment["boundaryInputSchema"]
            output_schema = fragment["boundaryOutputSchema"]
            raw_plan = {
                "inputSchema": input_schema,
                "outputSchema": output_schema,
                "steps": [
                    {
                        "id": "fragment",
                        "kind": "fragment",
                        "ref": body.refs[0],
                        "input": {
                            key: f"${{input.{key}}}"
                            for key in input_schema.get("properties", {})
                        },
                    }
                ],
                "output": {
                    key: f"${{fragment.{key}}}"
                    for key in output_schema.get("properties", {})
                },
            }
            snapshot = await create_plan_snapshot(session, raw_plan)
            run = await orchestrator.create_plan_run(session, snapshot, body.bindings)
            await session.commit()
            return {
                "action": body.action,
                "status": run.status,
                "runId": run.id,
                "planSnapshotId": snapshot.id,
            }
        assert body.plan is not None
        await require_plan_refs_disclosed(session, body.session_id, body.plan)
        snapshot = await create_plan_snapshot(session, body.plan)
        run = await orchestrator.create_plan_run(session, snapshot, body.bindings)
        await session.commit()
        return {
            "action": body.action,
            "status": run.status,
            "runId": run.id,
            "planSnapshotId": snapshot.id,
        }
    except (CatalogError, PlanCompileError, WorkflowValidationError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={
                "code": "SELECTION_EXECUTION_REJECTED",
                "action": body.action,
                "diagnostics": [{"message": str(exc)}],
            },
        ) from exc


def _plan_view(item: PlanSnapshot) -> dict[str, Any]:
    definition = item.definition_json.get("spec", {})
    nodes = definition.get("nodes", [])
    return {
        "id": item.id,
        "digest": item.digest,
        "status": item.status,
        "expiresAt": item.expires_at,
        "sourceRefs": item.source_refs_json,
        "locks": item.locks_json,
        "planSignature": item.plan_signature,
        "preview": {
            "inputSchema": definition.get("inputSchema"),
            "outputSchema": definition.get("outputSchema"),
            "steps": [
                {
                    "id": node["id"],
                    "name": node.get("displayName"),
                    "type": node["type"],
                    "sideEffect": bool(node.get("sideEffect")),
                }
                for node in nodes
                if node["type"] not in {"input", "output"}
                and "__" not in node["id"]
            ],
            "hasSideEffects": any(node.get("sideEffect") for node in nodes),
            "callsAgent": any(node["type"] in {"agent", "llm"} for node in nodes),
        },
    }


@router.post("/plans", status_code=201)
async def create_plan(
    body: PlanCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.created_by_conversation_run_id and await session.get(
        ConversationRun, body.created_by_conversation_run_id
    ) is None:
        raise HTTPException(404, "ConversationRun 不存在")
    try:
        await consume_session_call(
            session,
            body.session_id,
            "plan",
            {"planDigest": digest_json(body.plan)},
        )
        await require_plan_refs_disclosed(session, body.session_id, body.plan)
        snapshot = await create_plan_snapshot(
            session,
            body.plan,
            created_by_conversation_run_id=body.created_by_conversation_run_id,
            ttl_hours=body.ttl_hours,
        )
        await record_disclosure_response(
            session,
            body.session_id,
            "plan",
            {"planId": snapshot.id, "digest": snapshot.digest},
            level=None,
        )
    except (PlanCompileError, CatalogError, WorkflowValidationError) as exc:
        await session.rollback()
        raise HTTPException(
            422, detail={"code": "PLAN_COMPILATION_FAILED", "message": str(exc)}
        ) from exc
    await session.commit()
    return _plan_view(snapshot)


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    snapshot = await session.get(PlanSnapshot, plan_id)
    if snapshot is None:
        raise HTTPException(404, "PlanSnapshot 不存在")
    if expire_plan_snapshot(snapshot):
        await session.commit()
    return _plan_view(snapshot)


@router.post("/plans/{plan_id}/runs", status_code=202)
async def create_plan_run(
    plan_id: str,
    body: PlanRunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    snapshot = await session.get(PlanSnapshot, plan_id)
    if snapshot is None:
        raise HTTPException(404, "PlanSnapshot 不存在")
    try:
        run = await orchestrator.create_plan_run(session, snapshot, body.input)
    except (ValueError, CatalogError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_PLAN_RUN", "message": str(exc)}) from exc
    await session.commit()
    return run_view(run)


@router.get("/plan-suggestions")
async def list_plan_suggestions(
    threshold: int = Query(default=3, ge=2, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(PlanSnapshot))).all()
    changed = any(expire_plan_snapshot(item) for item in rows)
    if changed:
        await session.commit()
    rows = [item for item in rows if item.status == "active"]
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snapshot in rows:
        if snapshot.plan_signature in seen:
            continue
        matching = [
            item for item in rows if item.plan_signature == snapshot.plan_signature
        ]
        plan_ids = [item.id for item in matching]
        plan_runs = (
            await session.scalars(
                select(Run).where(Run.plan_snapshot_id.in_(plan_ids))
            )
        ).all()
        terminal = [
            run
            for run in plan_runs
            if run.status in {"succeeded", "failed", "cancelled"}
        ]
        success_count = sum(run.status == "succeeded" for run in terminal)
        failure_count = sum(run.status != "succeeded" for run in terminal)
        failure_rate = (
            round(failure_count / len(terminal), 3) if terminal else None
        )
        input_variants = len(
            {
                digest_json(redact(run.input_json))
                for run in terminal
                if run.status == "succeeded"
            }
        )
        run_ids = [run.id for run in terminal]
        metric_events = (
            (
                await session.scalars(
                    select(RunEvent).where(
                        RunEvent.run_id.in_(run_ids),
                        RunEvent.type == "node.completed",
                    )
                )
            ).all()
            if run_ids
            else []
        )
        total_tokens = sum(
            int((event.payload_json.get("metrics") or {}).get("totalTokens", 0))
            for event in metric_events
        )
        average_tokens = (
            round(total_tokens / success_count, 2) if success_count else None
        )
        manual_adjustments = max(0, len(matching) - 1)
        if success_count >= threshold and failure_count == 0 and input_variants >= 2:
            suggestions.append(
                {
                    "planSignature": snapshot.plan_signature,
                    "successCount": success_count,
                    "failureRate": failure_rate,
                    "inputVariantCount": input_variants,
                    "manualAdjustmentCount": manual_adjustments,
                    "averageTotalTokens": average_tokens,
                    "planSnapshotId": snapshot.id,
                    "action": "suggest_draft",
                }
            )
            seen.add(snapshot.plan_signature)
    return suggestions


@router.post("/plans/{plan_id}/solidify", status_code=201)
async def solidify_plan(
    plan_id: str,
    body: PlanSolidifyRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    snapshot = await session.get(PlanSnapshot, plan_id)
    if snapshot is None:
        raise HTTPException(404, "PlanSnapshot 不存在")
    expire_plan_snapshot(snapshot)
    try:
        verify_plan_snapshot(snapshot, for_new_run=True)
    except CatalogError as exc:
        await session.commit()
        raise HTTPException(
            422,
            detail={"code": "PLAN_NOT_SOLIDIFIABLE", "message": str(exc)},
        ) from exc
    definition = {
        **snapshot.definition_json,
        "metadata": {
            **snapshot.definition_json.get("metadata", {}),
            "name": body.name,
            "sourcePlanSnapshotId": snapshot.id,
            "sourceRefs": snapshot.source_refs_json,
        },
    }
    workflow = Workflow(
        id=str(uuid4()),
        name=body.name,
        draft_revision=1,
        draft_json=definition,
    )
    session.add(workflow)
    await session.commit()
    return {
        "workflowId": workflow.id,
        "draftRevision": workflow.draft_revision,
        "published": False,
        "sourcePlanSnapshotId": snapshot.id,
    }


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
    manifest = (
        await session.scalar(
            select(WorkflowCapabilityManifest).where(
                WorkflowCapabilityManifest.workflow_version_id == version.id
            )
        )
        if version
        else None
    )
    fragments = (
        (
            await session.scalars(
                select(WorkflowFragmentManifest).where(
                    WorkflowFragmentManifest.workflow_version_id == version.id,
                    WorkflowFragmentManifest.status == "published",
                )
            )
        ).all()
        if version
        else []
    )
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
        "capabilityManifest": (
            {
                **manifest.manifest_json,
                "manifestDigest": manifest.digest,
                "fragments": [
                    {
                        **fragment.manifest_json,
                        "manifestDigest": fragment.digest,
                    }
                    for fragment in fragments
                ],
            }
            if manifest
            else None
        ),
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
    version = (
        await session.get(WorkflowVersion, run.workflow_version_id)
        if run.workflow_version_id
        else None
    )
    plan_snapshot = (
        await session.get(PlanSnapshot, run.plan_snapshot_id)
        if run.plan_snapshot_id
        else None
    )
    source_definition = (
        version.definition_json if version else plan_snapshot.definition_json
        if plan_snapshot
        else {}
    )
    definition = source_definition.get("spec", {})
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
        if plan_snapshot:
            node["source"] = (plan_snapshot.source_map_json or {}).get(
                "expandedToSource", {}
            ).get(node["nodeId"])
    # 依赖版本：暴露 WorkflowVersion 锁定的 Capability/MCP digest 摘要。
    validation = (
        version.validation_json
        if version
        else {"capabilityLocks": plan_snapshot.locks_json}
        if plan_snapshot
        else {}
    )
    result["dependencyVersions"] = {
        "workflowVersionId": run.workflow_version_id,
        "workflowDigest": version.digest if version else None,
        "planSnapshotId": run.plan_snapshot_id,
        "planDigest": plan_snapshot.digest if plan_snapshot else None,
        "planStatus": plan_snapshot.status if plan_snapshot else None,
        "planExpiresAt": plan_snapshot.expires_at if plan_snapshot else None,
        "sourceMap": plan_snapshot.source_map_json if plan_snapshot else {},
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
            "approvalStatus": item.approval_status,
            "failedNode": item.failed_node_id,
            "attempt": item.attempt,
            "digest": item.digest,
            "completedNodes": sorted((item.snapshot_json or {}).get("completedOutputs", {})),
            "remainingSubgraph": (item.snapshot_json or {}).get("remainingSubgraph", []),
            "committedSideEffects": (item.snapshot_json or {}).get("committedSideEffects", []),
            "fallbackTarget": (item.snapshot_json or {}).get("fallbackConfig", {}).get("target"),
            "recoveryNodeRunId": item.recovery_node_run_id,
            "claimGeneration": item.claim_generation,
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


async def _approve_recovery(session: AsyncSession, approval: Approval) -> dict[str, Any]:
    """批准恢复：checkpoint→approved 且 Run→recovering，交给同一 Recovery claim。"""

    checkpoint_id = approval.snapshot_json.get("checkpointId")
    checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
    if checkpoint is None or checkpoint.status != "pending":
        raise HTTPException(409, "恢复检查点不可用或已处理")
    run = await session.get(Run, approval.run_id)
    if run is None or run.status != "waiting_approval":
        raise HTTPException(409, "运行不在等待恢复审批状态")
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    checkpoint.approval_status = "approved"
    run.status = "recovering"
    await append_event(
        session,
        approval.run_id,
        "run.recovery_requested",
        {"runId": run.id, "checkpointId": checkpoint.id, "failedNode": approval.node_id},
        approval.node_run_id,
    )
    await session.commit()
    return {"id": approval.id, "status": approval.status}


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
    effect = approval.snapshot_json.get("effect")
    # 恢复审批：批准后 checkpoint→approved 且 Run→recovering，由同一 Recovery claim 处理；
    # 关联的失败 NodeRun 保持 failed（不可变）。
    if effect == "recovery.approval":
        return await _approve_recovery(session, approval)
    node_run = await session.get(NodeRun, approval.node_run_id)
    if node_run is None or node_run.status != "waiting_user":
        raise HTTPException(409, "关联节点不在等待状态")
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
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


@router.post("/approvals/{approval_id}/answer")
async def answer_recovery_question(
    approval_id: str,
    body: RecoveryAnswer,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if (
        approval.status != "pending"
        or approval.snapshot_json.get("effect") != "recovery.question"
    ):
        raise HTTPException(409, "恢复问题不可回答或已处理")
    if approval.snapshot_digest != body.snapshot_digest:
        raise HTTPException(409, "恢复问题快照已变化")
    checkpoint = await session.get(
        ExecutionCheckpoint, approval.snapshot_json.get("checkpointId")
    )
    run = await session.get(Run, approval.run_id)
    if checkpoint is None or run is None or checkpoint.status != "awaiting_user":
        raise HTTPException(409, "恢复检查点不可继续")
    checkpoint.snapshot_json = {
        **checkpoint.snapshot_json,
        "userAnswer": redact(body.answer),
    }
    checkpoint.digest = digest_json(checkpoint.snapshot_json)
    checkpoint.status = "pending"
    checkpoint.decision_json = {
        **(checkpoint.decision_json or {}),
        "userAnswer": redact(body.answer),
    }
    checkpoint.lease_owner = None
    checkpoint.lease_expires_at = None
    run.status = "recovering"
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
    await append_event(
        session,
        run.id,
        "recovery.user_answered",
        {"checkpointId": checkpoint.id, "approvalId": approval.id},
    )
    await session.commit()
    return {"id": approval.id, "status": approval.status, "checkpointId": checkpoint.id}


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(404, "审批不存在")
    if approval.status != "pending":
        raise HTTPException(409, "审批已处理")
    # 恢复审批被拒绝：checkpoint→rejected，Run 保持原失败终态，失败历史不变。
    if approval.snapshot_json.get("effect") == "recovery.approval":
        checkpoint_id = approval.snapshot_json.get("checkpointId")
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        approval.status = "rejected"
        approval.decided_by = "local-user"
        approval.decided_at = utcnow()
        run = await session.get(Run, approval.run_id)
        failed_node = await session.get(NodeRun, approval.node_run_id)
        if checkpoint is not None:
            checkpoint.approval_status = "rejected"
            checkpoint.status = "rejected"
        if run is not None:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_json = failed_node.error_json if failed_node else {
                "code": "RECOVERY_REJECTED",
                "message": "恢复审批被拒绝",
            }
        await append_event(
            session,
            approval.run_id,
            "run.recovery_rejected",
            {"approvalId": approval.id, "checkpointId": checkpoint_id},
            approval.node_run_id,
        )
        await session.commit()
        return {"id": approval.id, "status": approval.status}
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
        previous_assistant = await session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        previous_content = previous_assistant.content_json if previous_assistant else {}
        disclosure = None
        if previous_content.get("state") == "ask_user":
            previous_session_id = previous_content.get("disclosureSessionId")
            disclosure = await session.get(
                WorkflowDisclosureSession, previous_session_id
            )
        if disclosure is None or disclosure.status != "active":
            disclosure = await create_disclosure_session(
                session,
                conversation_id=conversation.id,
                agent_run_id=f"selection:{uuid4()}",
            )
        selection_session_id = disclosure.id
        await consume_session_call(
            session,
            selection_session_id,
            "search",
            {"queryDigest": digest_json(body.content), "limit": 5},
        )
        workflow_cards = await search_manifests(session, body.content, limit=5)
        await disclose_refs(
            session,
            selection_session_id,
            [str(card["workflowRef"]) for card in workflow_cards],
            "L0",
        )
        await record_disclosure_response(
            session,
            selection_session_id,
            "search",
            {"items": workflow_cards},
            level="L0",
        )
        if workflow_cards:
            selection = await _select_workflow_candidate(
                body.content, workflow_cards, selection_session_id
            )
            if selection.action == "ask_user":
                assistant_content = {
                    "type": "workflow_selection",
                    "state": "ask_user",
                    "text": selection.question,
                    "required": selection.missing_inputs,
                    "agentAction": selection.action,
                    "disclosureSessionId": selection_session_id,
                    "disclosureLevel": "L0",
                }
            elif selection.action == "continue_agent":
                assistant_content, conversation_run = await _general_agent_message(
                    conversation, body.content
                )
                assistant_content = {
                    **assistant_content,
                    "agentAction": selection.action,
                    "selectionReason": selection.reason,
                    "disclosureSessionId": selection_session_id,
                }
            elif selection.action != "run_full":
                raise HTTPException(
                    422,
                    detail={
                        "code": "WORKFLOW_SELECTION_REJECTED",
                        "message": f"{selection.action} 需要先通过 Broker 披露 L1/L2 或提交 Plan",
                    },
                )
            else:
                selected_ref = selection.refs[0]
                if selected_ref not in {
                    str(card["workflowRef"]) for card in workflow_cards
                }:
                    raise HTTPException(
                        422,
                        detail={
                            "code": "WORKFLOW_SELECTION_REJECTED",
                            "message": "TraeX 选择了本次 L0 未披露的 ref",
                        },
                    )
                manifest = await describe_workflow(session, selected_ref)
                version = await session.get(
                    WorkflowVersion, manifest["versionLock"]["versionId"]
                )
                spec = version.definition_json["spec"]
                required = spec["inputSchema"].get("required", [])
                bindings = dict(selection.bindings)
                missing = [key for key in required if key not in bindings]
                if missing:
                    component = await session.scalar(
                        select(Component).where(
                            Component.default_version_id == version.id
                        )
                    )
                    if component is not None:
                        candidate = ComponentCandidate(
                            component_id=component.id,
                            component_name=component.name,
                            workflow_version_id=version.id,
                            input_schema=spec["inputSchema"],
                            ui_schema=spec.get("uiSchema", {}),
                            execution_class=component.execution_class,
                            confidence=1.0,
                            reason=selection.reason,
                        )
                        assistant_content = {
                            **candidate_message(candidate, missing=missing),
                            "workflowRef": selected_ref,
                            "selectionSource": "traex",
                            "agentAction": selection.action,
                            "selectionReason": selection.reason,
                            "disclosureSessionId": selection_session_id,
                            "disclosureLevel": "L0",
                        }
                    else:
                        assistant_content = {
                            "type": "workflow_selection",
                            "state": "ask_user",
                            "text": f"运行该 Workflow 还需要输入：{', '.join(missing)}",
                            "required": missing,
                            "inputSchema": spec["inputSchema"],
                            "workflowRef": selected_ref,
                            "agentAction": "ask_user",
                            "selectionReason": selection.reason,
                            "disclosureSessionId": selection_session_id,
                            "disclosureLevel": "L1",
                        }
                else:
                    run = await orchestrator.create_run(session, version, bindings)
                    assistant_content = {
                        "type": "workflow_run",
                        "state": "workflow_run",
                        "runId": run.id,
                        "workflowVersionId": version.id,
                        "workflowRef": selected_ref,
                        "agentAction": selection.action,
                        "selectionReason": selection.reason,
                        "disclosureSessionId": selection_session_id,
                        "disclosureLevel": "L1",
                        "text": "Workflow 已启动，后续执行由 Runtime 接管。",
                    }
        else:
            # 无发布 Manifest 时仅作为 UI 降级推荐，不代表 TraeX 已作语义选择。
            candidate = await match_component(session, body.content)
        if not workflow_cards and candidate is not None:
            assistant_content = candidate_message(candidate)
        elif not workflow_cards:
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


async def _select_workflow_candidate(
    user_text: str,
    cards: list[dict[str, Any]],
    session_id: str,
) -> WorkflowSelection:
    if not get_settings().traex_command:
        return WorkflowSelection(
            action="run_full",
            reason="无 TraeX Runtime，按 Registry 排序提供 UI 降级候选",
            refs=[str(cards[0]["workflowRef"])],
            sessionId=session_id,
        )
    selection_schema = WorkflowSelection.model_json_schema(by_alias=True)
    selection_schema["properties"].pop("sessionId", None)
    selection_schema["required"] = [
        item for item in selection_schema.get("required", []) if item != "sessionId"
    ]
    request = AgentHostRequest(
        run_id=f"workflow-selection-{uuid4()}",
        prompt=(
            "你负责从有界 L0 Workflow 卡片中做语义选择。不得选择列表外 ref，"
            "不得假设未披露的 Schema/DAG。输出 WorkflowSelection；信息不足时 ask_user，"
            "无合适候选时 continue_agent。\n"
            f"user={body_text(user_text)}\n"
            f"candidates={cards}"
        ),
        output_schema=selection_schema,
        working_directory=get_settings().artifact_root / "workflow-selections",
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=8000,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        selection = WorkflowSelection.model_validate(
            {**result.output, "sessionId": session_id}
        )
    except (AgentHostError, ValueError) as exc:
        raise HTTPException(
            502,
            detail={"code": "WORKFLOW_SELECTION_FAILED", "message": str(exc)},
        ) from exc
    return selection


def body_text(value: str) -> str:
    """限制进入选择 prompt 的用户文本长度。"""

    return value[:2000]


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
