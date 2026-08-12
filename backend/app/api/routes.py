from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    resolve_local_demo_capabilities,
)
from app.application.agent_provider import (
    llm_runtime_readiness,
    probe_provider,
    provider_readiness,
)
from app.application.agent_run import (
    AgentRunConfig,
    build_agent_run_message,
    enqueue_agent_run,
    request_cancellation,
    summarize_run,
)
from app.application.agent_run import _append_event as append_agent_event
from app.application.browser_readiness import browser_runtime_readiness
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
)
from app.application.distillation import (
    DistillationError,
    distill_conversation_run,
)
from app.application.error_policy import public_error, technical_error
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
    compile_plan,
    create_plan_snapshot,
    expire_plan_snapshot,
    verify_plan_snapshot,
)
from app.application.product_history import (
    bind_presentation_decision,
    finalize_turn,
    normalize_presentation_policy,
    presentation_policy,
    presentation_view,
    record_model_usage,
    turn_view,
    validate_presentation_decision,
    validate_presentation_template,
)
from app.application.replay import run_isolated_replay, run_replay_proof
from app.application.retries import auto_retry_allowed
from app.application.runtime_config import (
    agent_runtime_config,
    semantic_workflow_selection_available,
)
from app.application.secrets import (
    SecretResolutionError,
    register_secret_reference,
)
from app.application.security import redact
from app.application.tool_broker import ToolAdapterRegistry
from app.application.workflow_catalog import (
    CatalogError,
    build_manifest,
    capability_metadata_issues,
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
    PATCHABLE_EVIDENCE_KINDS,
    TRANSPORT_EVIDENCE_KINDS,
    WorkflowPatch,
    WorkflowPatchViolation,
    apply_workflow_patch,
    observation_eligibility,
)
from app.application.workflow_tool_broker import workflow_catalog_adapter
from app.config import get_settings
from app.infrastructure.database import (
    Approval,
    Artifact,
    Capability,
    Component,
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    ConversationTurn,
    Distillation,
    ExecutionCheckpoint,
    FailureObservation,
    HumanPatch,
    MCPServer,
    MCPToolSnapshot,
    Message,
    ModelUsage,
    NodeRun,
    PendingSelection,
    PlanSnapshot,
    ReplayEvidence,
    ReplayFixture,
    RevisionProposal,
    Run,
    RunEvent,
    RunPresentation,
    SecretReference,
    TaskCheckpoint,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowFragmentManifest,
    WorkflowVersion,
    get_session,
    rfc3339,
    utcnow,
)
from app.infrastructure.migrations import alembic_head_revision
from app.runtime.contract_patch import ContractPatchViolation, apply_contract_patch

router = APIRouter()
compiler = WorkflowCompiler()
orchestrator = Orchestrator()


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]


class DraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    definition: dict[str, Any]


class PublishRequest(BaseModel):
    draft_revision: int = Field(alias="draftRevision", ge=1)
    draft_digest: str = Field(alias="draftDigest", min_length=8, max_length=80)
    replay_evidence_id: str | None = Field(
        default=None, alias="replayEvidenceId"
    )


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


class TaskCheckpointAnswer(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")
    answer: dict[str, Any]


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


class ConversationModeUpdate(BaseModel):
    workflow_mode: str = Field(alias="workflowMode", pattern="^(enabled|disabled)$")


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


def _resolve_agent_capabilities(
    requested: list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    normalized = tuple(requested) if requested is not None else None
    effective, denied = resolve_local_demo_capabilities(normalized)
    if denied:
        raise HTTPException(
            422,
            detail={
                "code": "AGENT_CAPABILITY_DENIED",
                "message": "请求包含 Local Demo 只读探索策略未授予的能力",
                "deniedCapabilities": list(denied),
                "effectiveCapabilities": list(effective),
            },
        )
    if not effective:
        raise HTTPException(
            422,
            detail={
                "code": "AGENT_CAPABILITY_UNAVAILABLE",
                "message": "当前 Host 未启用任何受控只读探索能力",
            },
        )
    return effective


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


async def _record_human_definition_patch(
    session: AsyncSession,
    *,
    proposal_id: str,
    definition: dict[str, Any],
    editor_id: str,
) -> HumanPatch:
    payload = {"kind": "DraftDefinition", "definition": definition}
    patch_digest = digest_json(payload)
    existing = await session.scalar(
        select(HumanPatch).where(
            HumanPatch.parent_proposal_id == proposal_id,
            HumanPatch.patch_digest == patch_digest,
        )
    )
    if existing is not None:
        return existing
    item = HumanPatch(
        id=str(uuid4()),
        parent_proposal_id=proposal_id,
        editor_id=editor_id,
        patch_json=payload,
        patch_digest=patch_digest,
    )
    session.add(item)
    await session.flush()
    return item


async def _commit_revision_proposal(
    session: AsyncSession,
    proposal: RevisionProposal,
) -> RevisionProposal:
    session.add(proposal)
    try:
        await session.commit()
        return proposal
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(RevisionProposal).where(
                RevisionProposal.observation_id == proposal.observation_id,
                RevisionProposal.baseline_digest == proposal.baseline_digest,
                RevisionProposal.proposal_digest == proposal.proposal_digest,
            )
        )
        if existing is not None:
            return existing
        raise HTTPException(
            409,
            detail={
                "code": "REVISION_PROPOSAL_CONFLICT",
                "message": "并发 proposal 与当前提议不一致",
            },
        ) from None


async def _mark_draft_changed(
    session: AsyncSession,
    workflow: Workflow,
    definition: dict[str, Any],
    *,
    editor_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = dict(workflow.draft_metadata_json or {})
    status = metadata.get("status")
    if status not in {"pending_revision", "dirty_after_replay"}:
        metadata["status"] = "manual_draft"
        return definition, metadata

    lineage = dict(definition.get("metadata", {}).get("revisionLineage") or {})
    if lineage:
        lineage["status"] = "superseded"
        definition = {
            **definition,
            "metadata": {
                **definition.get("metadata", {}),
                "revisionLineage": lineage,
            },
        }
    proposal_id = metadata.get("proposalId")
    if isinstance(proposal_id, str):
        human_patch = await _record_human_definition_patch(
            session,
            proposal_id=proposal_id,
            definition=definition,
            editor_id=editor_id,
        )
        metadata["humanPatchId"] = human_patch.id
        metadata["humanPatchDigest"] = human_patch.patch_digest
    metadata.pop("replayApprovedDraftDigest", None)
    metadata["status"] = "dirty_after_replay"
    metadata["supersededReplayEvidenceId"] = metadata.get("replayEvidenceId")
    return definition, metadata


def workflow_view(item: Workflow) -> dict[str, Any]:
    draft_digest = item.draft_digest
    if draft_digest is None:
        try:
            draft_digest = compiler.compile(item.draft_json).digest
        except WorkflowValidationError:
            draft_digest = digest_json(item.draft_json)
    return {
        "id": item.id,
        "name": item.name,
        "draftRevision": item.draft_revision,
        "draftDigest": draft_digest,
        "draftBaseVersionId": item.draft_base_version_id,
        "draftMetadata": item.draft_metadata_json,
        "definition": item.draft_json,
        "createdAt": rfc3339(item.created_at),
        "updatedAt": rfc3339(item.updated_at),
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
        "turnId": run.turn_id,
        "status": run.status,
        "input": redact(run.input_json, input_schema),
        "output": redact(run.output_json, output_schema),
        "error": public_error(redact(run.error_json)),
        "createdAt": rfc3339(run.created_at),
        "startedAt": rfc3339(run.started_at),
        "finishedAt": rfc3339(run.finished_at),
        "durationMs": _duration_ms(run.started_at, run.finished_at),
        "runtimeSnapshot": run.runtime_snapshot_json or {},
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
                "error": public_error(redact(item.error_json)),
                "createdAt": rfc3339(item.created_at),
                "updatedAt": rfc3339(item.updated_at),
                "startedAt": rfc3339(item.started_at),
                "finishedAt": rfc3339(item.finished_at),
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
async def health_ready(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
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
    provider = await provider_readiness()
    llm_runtime = await llm_runtime_readiness()
    browser_runtime = await browser_runtime_readiness(session)
    worker_available = bool(
        browser_runtime.get("workerId") and not browser_runtime.get("stale")
    )
    return {
        "status": "ready",
        "migration": revision,
        "workflowSelectionMode": (
            "real"
            if semantic_workflow_selection_available(
                probe_verified=bool(provider.get("modelAvailable"))
            )
            else "unavailable"
        ),
        "selectionProvider": provider,
        "agentRuntime": provider,
        "llmRuntime": llm_runtime,
        "modelRuntimeFallbackPolicy": {
            "order": ["llm_runtime", "agent_runtime"],
            "trigger": "startup_unavailable_only",
            "executionErrorsFallback": False,
        },
        "runtimes": {
            "database": {"status": "available", "detail": "SQLite 与迁移可用"},
            "worker": {
                "status": "available" if worker_available else "unknown",
                "detail": (
                    f"Worker {browser_runtime['workerId']} 已写入运行时心跳"
                    if worker_available
                    else "未发现新鲜的独立 Worker 心跳"
                ),
            },
            "browser": browser_runtime,
            "secretProvider": {
                "status": "reference_only",
                "detail": "仅解析环境变量引用，不存储明文",
            },
        },
    }


@router.post("/health/provider-probe")
async def run_provider_probe(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Run the explicit, rate-limited paid compatibility probe."""

    try:
        result = await probe_provider()
    except ValueError as exc:
        raise HTTPException(
            409,
            detail={"code": "PROVIDER_PROBE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    probe_id = result.get("probeId")
    if isinstance(probe_id, str):
        await record_model_usage(
            session,
            call_id=f"operational-probe:{probe_id}",
            stage="operational_probe",
            provider=result.get("provider"),
            model=result.get("model"),
            protocol=result.get("protocol"),
            runtime_type=result.get("runtimeType"),
            adapter_version=result.get("adapterVersion"),
            adapter_digest=result.get("sidecarDigest"),
            runtime_used="agent_runtime",
            runtime_digest=result.get("sidecarDigest"),
            error_code=result.get("code"),
            input_tokens=result.get("inputTokens"),
            output_tokens=result.get("outputTokens"),
            total_tokens=result.get("totalTokens"),
            usage_complete=bool(result.get("usageComplete")),
            dispatched=bool(result.get("requestDispatched")),
            completed=bool(result.get("completed")),
        )
        await session.commit()
    return result


@router.get("/workflows")
async def list_workflows(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (await session.scalars(select(Workflow).order_by(Workflow.updated_at.desc()))).all()
    return [workflow_view(item) for item in rows]


@router.post("/workflows", status_code=201)
async def create_workflow(
    body: WorkflowCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        compiled = compiler.compile(body.definition)
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
        draft_digest=compiled.digest,
        draft_metadata_json={"status": "manual_draft"},
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


@router.get("/workflows/{workflow_id}/versions")
async def list_workflow_versions(
    workflow_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(404, "工作流不存在")
    rows = (
        await session.scalars(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
    ).all()
    return [
        {
            "id": item.id,
            "workflowId": item.workflow_id,
            "version": item.version,
            "digest": item.digest,
            "definition": item.definition_json,
            "presentationPolicy": item.presentation_policy_json,
            "createdAt": rfc3339(item.created_at),
        }
        for item in rows
    ]


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
    try:
        compiled = compiler.compile(body.definition)
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "WORKFLOW_VALIDATION_FAILED",
                "diagnostics": [
                    diagnostic.model_dump() for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    definition = compiled.definition.model_dump(by_alias=True, mode="json")
    try:
        bind_presentation_decision(definition["metadata"])
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "PRESENTATION_DECISION_INVALID", "message": str(exc)},
        ) from exc
    compiled = compiler.compile(definition)
    current_digest = item.draft_digest or compiler.compile(item.draft_json).digest
    if compiled.digest == current_digest:
        if item.draft_digest is None:
            item.draft_digest = current_digest
            await session.commit()
            await session.refresh(item)
        return workflow_view(item)
    definition, metadata = await _mark_draft_changed(
        session,
        item,
        compiled.definition.model_dump(by_alias=True, mode="json"),
        editor_id="local-user",
    )
    item.draft_json = definition
    item.draft_digest = compiler.compile(definition).digest
    item.draft_revision += 1
    item.draft_metadata_json = metadata
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
        metadata_issues = capability_metadata_issues(compiled.definition)
        if metadata_issues:
            return {
                "valid": False,
                "diagnostics": [
                    {
                        "level": "error",
                        "code": "CAPABILITY_METADATA_REQUIRED",
                        "path": path,
                        "message": "可发布能力必须提供完整、非空的语义目录元数据",
                    }
                    for path in metadata_issues
                ],
            }
        execution_class, capability_locks = await validate_workflow_capabilities(
            session, compiled.definition
        )
        mcp_locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
        try:
            validate_presentation_decision(compiled.definition.metadata)
        except ValueError as exc:
            return {
                "valid": False,
                "diagnostics": [
                    {
                        "level": "error",
                        "code": "PRESENTATION_DECISION_REQUIRED",
                        "path": "metadata.presentationPolicyDecision",
                        "message": str(exc),
                    }
                ],
            }
        policy = compiled.definition.metadata.get("presentationPolicy", {})
        if (
            isinstance(policy, dict)
            and policy.get("enabled") is True
            and policy.get("producer", "template") == "template"
        ):
            validate_presentation_template(
                str(policy.get("template", "执行结果：{json}")),
                compiled.definition.spec.output_schema,
            )
        return {
            "valid": True,
            "digest": compiled.digest,
            "topologicalOrder": compiled.topological_order,
            "diagnostics": [
                {
                    "level": diagnostic.level,
                    "code": diagnostic.code,
                    "path": diagnostic.path,
                    "message": diagnostic.message,
                }
                for diagnostic in compiled.diagnostics
            ],
            "executionClass": execution_class,
            "capabilityLocks": capability_locks,
            "mcpLocks": mcp_locks,
        }
    except WorkflowValidationError as exc:
        return {
            "valid": False,
            "diagnostics": [
                {
                    "level": diagnostic.level,
                    "code": diagnostic.code,
                    "path": diagnostic.path,
                    "message": diagnostic.message,
                }
                for diagnostic in exc.diagnostics
            ],
        }
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
    except (TypeError, ValueError) as exc:
        return {
            "valid": False,
            "diagnostics": [
                {
                    "level": "error",
                    "code": "PRESENTATION_TEMPLATE_INVALID",
                    "path": "metadata.presentationPolicy.template",
                    "message": str(exc),
                }
            ],
        }


@router.post("/workflows/{workflow_id}/publish", status_code=201)
async def publish_workflow(
    workflow_id: str,
    body: PublishRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    try:
        compiled = compiler.compile(item.draft_json)
        metadata_issues = capability_metadata_issues(compiled.definition)
        if metadata_issues:
            raise HTTPException(
                422,
                detail={
                    "code": "CAPABILITY_METADATA_REQUIRED",
                    "message": "目录公开前必须补充完整语义元数据",
                    "fields": metadata_issues,
                },
            )
        execution_class, capability_locks = await validate_workflow_capabilities(
            session, compiled.definition
        )
        mcp_locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
        validate_presentation_decision(compiled.definition.metadata)
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "WORKFLOW_VALIDATION_FAILED",
                "diagnostics": [
                    {
                        "level": diagnostic.level,
                        "code": diagnostic.code,
                        "path": diagnostic.path,
                        "message": diagnostic.message,
                    }
                    for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    except (CapabilityResolutionError, MCPConfigurationError) as exc:
        raise HTTPException(
            422,
            detail={"code": "CAPABILITY_INCOMPATIBLE", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "PRESENTATION_DECISION_REQUIRED", "message": str(exc)},
        ) from exc

    async def published_response(version: WorkflowVersion) -> dict[str, Any]:
        manifest = await session.scalar(
            select(WorkflowCapabilityManifest).where(
                WorkflowCapabilityManifest.workflow_version_id == version.id
            )
        )
        if manifest is None:
            raise HTTPException(
                409,
                detail={
                    "code": "PUBLISH_INCOMPLETE",
                    "message": "已发布版本缺少 Capability Manifest",
                },
            )
        return {
            "id": version.id,
            "workflowId": version.workflow_id,
            "version": version.version,
            "digest": version.digest,
            "definition": version.definition_json,
            "workflowRef": manifest.manifest_json["workflowRef"],
            "manifestDigest": manifest.digest,
            "publishedFromDraftRevision": version.published_from_draft_revision,
            "publishedFromDraftDigest": version.published_from_draft_digest,
        }

    existing = await session.scalar(
        select(WorkflowVersion).where(
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.published_from_draft_revision
            == body.draft_revision,
            WorkflowVersion.published_from_draft_digest == body.draft_digest,
        )
    )
    if existing is not None:
        return await published_response(existing)
    if (
        item.draft_revision != body.draft_revision
        or compiled.digest != body.draft_digest
    ):
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_SNAPSHOT_CONFLICT",
                "message": "草稿已变化，发布请求不是当前审核快照",
                "currentRevision": item.draft_revision,
                "currentDigest": compiled.digest,
            },
        )

    revision_metadata = dict(item.draft_metadata_json or {})
    revision_status = revision_metadata.get("status")
    if revision_status == "dirty_after_replay":
        raise HTTPException(
            409,
            detail={
                "code": "REPLAY_PROOF_STALE",
                "message": "Replay 后草稿已变化，必须重新 Replay",
            },
        )
    expected_base_id = revision_metadata.get("baseVersionId")
    if revision_status == "pending_revision":
        evidence_id = revision_metadata.get("replayEvidenceId")
        evidence = (
            await session.get(ReplayEvidence, evidence_id)
            if isinstance(evidence_id, str)
            else None
        )
        if (
            body.replay_evidence_id != evidence_id
            or evidence is None
            or not evidence.passed
            or evidence.patched_definition_digest != compiled.digest
            or revision_metadata.get("replayApprovedDraftDigest")
            != compiled.digest
            or evidence.base_version_id != expected_base_id
            or evidence.patch_digest != revision_metadata.get("patchDigest")
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "REPLAY_PROOF_STALE",
                    "message": "Replay proof 与当前草稿、Patch 或 base version 不一致",
                },
            )
        latest_version_id = await session.scalar(
            select(WorkflowVersion.id)
            .where(WorkflowVersion.workflow_id == item.id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
        if latest_version_id != expected_base_id:
            raise HTTPException(
                409,
                detail={
                    "code": "DRAFT_BASE_CONFLICT",
                    "message": "修订草稿的 base version 已不是当前最新版本",
                },
            )

    publishing_metadata = {
        **revision_metadata,
        "status": "publishing",
        "publishingDraftRevision": body.draft_revision,
        "publishingDraftDigest": body.draft_digest,
    }
    status_expression = func.json_extract(
        Workflow.draft_metadata_json, "$.status"
    )
    status_clause = (
        status_expression == revision_status
        if revision_status is not None
        else status_expression.is_(None)
    )
    claimed = await session.execute(
        update(Workflow)
        .where(
            Workflow.id == workflow_id,
            Workflow.draft_revision == body.draft_revision,
            or_(
                Workflow.draft_digest == body.draft_digest,
                Workflow.draft_digest.is_(None),
            ),
            status_clause,
        )
        .values(
            draft_digest=body.draft_digest,
            draft_metadata_json=publishing_metadata,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        existing = await session.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.published_from_draft_revision
                == body.draft_revision,
                WorkflowVersion.published_from_draft_digest
                == body.draft_digest,
            )
        )
        if existing is not None:
            return await published_response(existing)
        raise HTTPException(
            409,
            detail={
                "code": "PUBLISH_CONFLICT",
                "message": "草稿已被并发修改或发布",
            },
        )

    latest = await session.scalar(
        select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == item.id)
    )
    lineage = dict(
        compiled.definition.metadata.get("revisionLineage", {})
    )
    try:
        frozen_presentation_policy = normalize_presentation_policy(
            compiled.definition.metadata,
            execution_class=execution_class,
            output_schema=compiled.definition.spec.output_schema,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            422,
            detail={
                "code": "PRESENTATION_POLICY_INVALID",
                "message": str(exc),
                "field": "metadata.presentationPolicy",
            },
        ) from exc
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
        published_from_draft_revision=body.draft_revision,
        published_from_draft_digest=body.draft_digest,
        revision_lineage_json=lineage,
        presentation_policy_json=frozen_presentation_policy,
    )
    session.add(version)
    await session.flush()
    try:
        await build_manifest(session, item, version)
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={"code": "CAPABILITY_MANIFEST_INVALID", "message": str(exc)},
        ) from exc
    published_metadata = {
        **revision_metadata,
        "status": "published",
        "publishedVersionId": version.id,
        "publishedVersion": version.version,
        "publishedFromDraftRevision": body.draft_revision,
        "publishedFromDraftDigest": body.draft_digest,
    }
    finalized = await session.execute(
        update(Workflow)
        .where(
            Workflow.id == workflow_id,
            Workflow.draft_revision == body.draft_revision,
            func.json_extract(
                Workflow.draft_metadata_json, "$.status"
            )
            == "publishing",
            func.json_extract(
                Workflow.draft_metadata_json, "$.publishingDraftDigest"
            )
            == body.draft_digest,
        )
        .values(
            draft_base_version_id=version.id,
            draft_json=version.definition_json,
            draft_digest=version.digest,
            draft_metadata_json=published_metadata,
        )
        .execution_options(synchronize_session=False)
    )
    if finalized.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            409,
            detail={
                "code": "PUBLISH_CONFLICT",
                "message": "发布领取已失效",
            },
        )
    await session.commit()
    await session.refresh(version)
    return await published_response(version)


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
        compiled = compiler.compile(patched)
    except ContractPatchViolation as exc:
        raise HTTPException(
            422,
            detail={"code": "CONTRACT_PATCH_REJECTED", "message": str(exc)},
        ) from exc
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "WORKFLOW_VALIDATION_FAILED",
                "diagnostics": [
                    diagnostic.model_dump() for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    current_digest = (
        workflow.draft_digest or compiler.compile(workflow.draft_json).digest
    )
    if compiled.digest == current_digest:
        workflow.draft_digest = current_digest
        return {
            "id": workflow.id,
            "draftRevision": workflow.draft_revision,
            "draftDigest": current_digest,
            "definition": workflow.draft_json,
        }
    patched, metadata = await _mark_draft_changed(
        session,
        workflow,
        compiled.definition.model_dump(by_alias=True, mode="json"),
        editor_id="local-user",
    )
    workflow.draft_json = patched
    workflow.draft_digest = compiler.compile(patched).digest
    workflow.draft_revision += 1
    workflow.draft_metadata_json = metadata
    await session.commit()
    return {
        "id": workflow.id,
        "draftRevision": workflow.draft_revision,
        "draftDigest": workflow.draft_digest,
        "definition": patched,
    }


async def _load_replay_fixtures(
    session: AsyncSession, source_run: Run
) -> tuple[dict[str, dict[str, Any]], str | None]:
    if source_run.workflow_version_id is not None:
        execution_source = await session.get(
            WorkflowVersion, source_run.workflow_version_id
        )
    else:
        execution_source = await session.get(PlanSnapshot, source_run.plan_snapshot_id)
    if execution_source is None:
        raise HTTPException(
            409,
            detail={
                "code": "REPLAY_EVIDENCE_CORRUPT",
                "message": "Replay 执行源不存在",
            },
        )
    definition = execution_source.definition_json
    nodes = {
        str(node.get("id")): node
        for node in definition.get("spec", {}).get("nodes", [])
        if isinstance(node, dict)
    }
    rows = (
        await session.execute(
            select(ReplayFixture, Artifact)
            .join(Artifact, Artifact.id == ReplayFixture.artifact_id)
            .where(ReplayFixture.run_id == source_run.id)
        )
    ).all()
    fixtures: dict[str, dict[str, Any]] = {}
    locked: list[dict[str, str]] = []
    root = (get_settings().artifact_root / source_run.id).resolve()
    for fixture, artifact in rows:
        storage_key = (artifact.metadata_json or {}).get("storageKey")
        if not storage_key:
            continue
        path = (root / storage_key).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise HTTPException(
                409,
                detail={
                    "code": "REPLAY_EVIDENCE_CORRUPT",
                    "message": f"Replay fixture {fixture.id} 完整性校验失败",
                },
            )
        payload = json.loads(content)
        node = nodes.get(fixture.node_id)
        node_run = await session.get(NodeRun, fixture.node_run_id)
        expected_source_digest = (
            digest_json(
                {
                    "type": node.get("type"),
                    "config": node.get("config", {}),
                }
            )
            if node is not None
            else None
        )
        if (
            node_run is None
            or node_run.run_id != source_run.id
            or node_run.node_id != fixture.node_id
            or expected_source_digest != fixture.source_digest
            or digest_json(redact(node_run.input_json)) != fixture.input_digest
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "REPLAY_EVIDENCE_CORRUPT",
                    "message": f"Replay fixture {fixture.id} 来源或输入 digest 不匹配",
                },
            )
        status = payload.get("status")
        if status == "failed":
            evidence_value = {
                "error": payload.get("error"),
                "observedOutput": payload.get("observedOutput"),
            }
            valid_payload = isinstance(payload.get("error"), dict)
        elif status == "succeeded":
            evidence_value = payload.get("output")
            valid_payload = isinstance(evidence_value, dict)
        else:
            evidence_value = payload.get("output")
            valid_payload = isinstance(evidence_value, dict)
            payload = {"status": "succeeded", "output": evidence_value}
        if (
            not valid_payload
            or digest_json(evidence_value) != fixture.output_digest
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "REPLAY_EVIDENCE_CORRUPT",
                    "message": f"Replay fixture {fixture.id} output/error digest 不匹配",
                },
            )
        fixtures[fixture.node_id] = payload
        fixtures[fixture.node_id]["evidenceKind"] = fixture.evidence_kind
        locked.append(
            {
                "nodeId": fixture.node_id,
                "sourceDigest": fixture.source_digest,
                "inputDigest": fixture.input_digest,
                "outputDigest": fixture.output_digest,
                "evidenceKind": fixture.evidence_kind,
            }
        )
    return fixtures, digest_json(locked) if locked else None


@router.post("/failure-observations/{observation_id}/revision-proposal")
async def propose_failure_revision(
    observation_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    observation = await session.get(FailureObservation, observation_id)
    if observation is None:
        raise HTTPException(404, "FailureObservation 不存在")
    if observation.workflow_version_id is None or observation.last_run_id is None:
        raise HTTPException(
            422,
            detail={
                "code": "REVISION_SOURCE_UNSUPPORTED",
                "message": "当前 Revision Agent 只接受锁定 WorkflowVersion 的 source Run",
            },
        )
    version = await session.get(WorkflowVersion, observation.workflow_version_id)
    source_run = await session.get(Run, observation.last_run_id)
    if version is None or source_run is None:
        raise HTTPException(422, "修订来源已不存在")
    fixtures, fixture_digest = await _load_replay_fixtures(session, source_run)
    baseline = await run_isolated_replay(
        version.definition_json,
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        fixtures=fixtures,
    )
    baseline_matches = (
        baseline["failedNodeStatus"] == "failed"
        and baseline["failedNodeErrorCode"] == observation.error_code
    )
    observation.baseline_reproduced = bool(
        baseline_matches
        and observation.evidence_kind in PATCHABLE_EVIDENCE_KINDS
        and observation.evidence_kind not in TRANSPORT_EVIDENCE_KINDS
    )
    eligibility = observation_eligibility(observation)
    if not eligibility["eligible"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": "REVISION_NOT_ELIGIBLE",
                "message": eligibility["reason"],
                "baseline": baseline,
                "fixtureDigest": fixture_digest,
            },
        )
    baseline_digest = digest_json(
        {
            "baseDefinitionDigest": version.digest,
            "fixtureDigest": fixture_digest,
            "baseline": {
                key: value
                for key, value in baseline.items()
                if key != "replayRunId"
            },
        }
    )
    existing = await session.scalar(
        select(RevisionProposal)
        .where(
            RevisionProposal.observation_id == observation.id,
            RevisionProposal.base_version_id == version.id,
            RevisionProposal.baseline_digest == baseline_digest,
            RevisionProposal.fixture_digest == fixture_digest,
            RevisionProposal.status == "proposed",
        )
        .order_by(RevisionProposal.created_at.desc())
    )
    if existing is not None:
        proposal = WorkflowPatch.model_validate(existing.proposal_json)
        patched = apply_workflow_patch(version.definition_json, proposal)
        compiled = compiler.compile(patched)
        await session.commit()
        return {
            "proposalId": existing.id,
            "observationId": observation.id,
            "baseVersionId": version.id,
            "agentRunId": existing.agent_run_id,
            "promptVersion": existing.prompt_version,
            "fixtureDigest": fixture_digest,
            "baselineDigest": baseline_digest,
            "baseline": baseline,
            "proposal": existing.proposal_json,
            "proposalDigest": existing.proposal_digest,
            "diagnostics": [
                item.model_dump() for item in compiled.diagnostics
            ],
            "published": False,
        }
    failed_node = next(
        (
            node
            for node in version.definition_json.get("spec", {}).get(
                "nodes", []
            )
            if node.get("id") == observation.node_id
        ),
        {},
    )
    agent_run_id = f"revision-{observation.id}-{uuid4()}"
    prompt_version = "revision-agent-v1"
    settings = get_settings()
    request = AgentHostRequest(
        run_id=agent_run_id,
        prompt=(
            "Revision Agent。只输出受 WorkflowPatch Schema 限制的提议，不得扩大能力锁、"
            "副作用、权限或模型；不得发布版本。\n"
            f"baseVersionId={version.id}\n"
            f"failedNodeId={observation.node_id}\n"
            f"failure={observation.error_code}\n"
            f"locks={observation.locks_json}\n"
            "failedNodeConfig="
            f"{json.dumps(redact(failed_node.get('config', {})), ensure_ascii=False)}\n"
            f"replayDiagnostics={baseline.get('schemaDiagnostics', [])}"
        ),
        output_schema=WorkflowPatch.model_json_schema(by_alias=True),
        working_directory=get_settings().artifact_root / "revisions" / observation.id,
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=settings.agent_max_total_tokens,
        max_input_tokens=settings.agent_max_input_tokens,
        max_output_tokens=settings.agent_max_output_tokens,
        max_billable_tokens=settings.agent_max_billable_tokens,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        proposal = WorkflowPatch.model_validate(result.output)
        if proposal.base_version_id != version.id:
            raise WorkflowPatchViolation("Revision Agent 修改了 baseVersionId")
        patched = apply_workflow_patch(version.definition_json, proposal)
        compiler.compile(patched)
    except (
        AgentHostError,
        ValueError,
        WorkflowPatchViolation,
        WorkflowValidationError,
    ) as exc:
        raise HTTPException(
            422,
            detail={"code": "REVISION_PROPOSAL_REJECTED", "message": str(exc)},
        ) from exc
    proposal_json = proposal.model_dump(by_alias=True)
    proposal_digest = digest_json(proposal_json)
    proposal_record = RevisionProposal(
        id=str(uuid4()),
        observation_id=observation.id,
        base_version_id=version.id,
        agent_run_id=agent_run_id,
        prompt_version=prompt_version,
        fixture_digest=fixture_digest,
        baseline_digest=baseline_digest,
        proposal_json=proposal_json,
        proposal_digest=proposal_digest,
        status="proposed",
    )
    proposal_record = await _commit_revision_proposal(
        session, proposal_record
    )
    persisted_proposal = WorkflowPatch.model_validate(
        proposal_record.proposal_json
    )
    persisted_patched = apply_workflow_patch(
        version.definition_json, persisted_proposal
    )
    persisted_compiled = compiler.compile(persisted_patched)
    return {
        "proposalId": proposal_record.id,
        "observationId": proposal_record.observation_id,
        "baseVersionId": proposal_record.base_version_id,
        "agentRunId": proposal_record.agent_run_id,
        "promptVersion": proposal_record.prompt_version,
        "fixtureDigest": proposal_record.fixture_digest,
        "baselineDigest": proposal_record.baseline_digest,
        "baseline": baseline,
        "proposal": proposal_record.proposal_json,
        "proposalDigest": proposal_record.proposal_digest,
        "diagnostics": [
            item.model_dump() for item in persisted_compiled.diagnostics
        ],
        "published": False,
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
    proposal_id = body.get("proposalId")
    proposal_digest = body.get("proposalDigest")
    proposal = (
        await session.get(RevisionProposal, proposal_id)
        if isinstance(proposal_id, str)
        else None
    )
    if (
        proposal is None
        or proposal.proposal_digest != proposal_digest
        or proposal.base_version_id != version_id
    ):
        raise HTTPException(
            409,
            detail={
                "code": "REVISION_PROPOSAL_MISMATCH",
                "message": "proposalId/digest 与目标版本不匹配",
            },
        )
    observation = await session.get(
        FailureObservation, proposal.observation_id
    )
    if observation is None or observation.workflow_version_id != version_id:
        raise HTTPException(
            409,
            detail={
                "code": "REVISION_PROPOSAL_MISMATCH",
                "message": "proposal 的 FailureObservation 已失效",
            },
        )
    source_run_id = body.get("sourceRunId")
    if not source_run_id or source_run_id != observation.last_run_id:
        raise HTTPException(
            409,
            detail={
                "code": "PATCH_SOURCE_MISMATCH",
                "message": "必须使用 proposal 绑定 observation 的 sourceRunId",
            },
        )
    source_run = await session.get(Run, source_run_id)
    if source_run is None or source_run.workflow_version_id != version_id:
        raise HTTPException(
            409,
            detail={
                "code": "PATCH_SOURCE_MISMATCH",
                "message": "source Run 与目标 WorkflowVersion 不一致",
            },
        )

    patch_body = body.get("patch")
    if patch_body is None:
        patch_body = proposal.proposal_json
    try:
        patch = WorkflowPatch.model_validate(
            {**patch_body, "baseVersionId": version_id}
        )
        patch_json = patch.model_dump(by_alias=True)
        submitted_digest = digest_json(patch_json)
        human_patch: HumanPatch | None = None
        if submitted_digest != proposal.proposal_digest:
            if body.get("humanEdit") is not True:
                raise HTTPException(
                    409,
                    detail={
                        "code": "REVISION_PROPOSAL_TAMPERED",
                        "message": "修改 Agent proposal 必须显式声明 humanEdit",
                    },
                )
            human_patch = await session.scalar(
                select(HumanPatch).where(
                    HumanPatch.parent_proposal_id == proposal.id,
                    HumanPatch.patch_digest == submitted_digest,
                )
            )
            if human_patch is None:
                human_patch = HumanPatch(
                    id=str(uuid4()),
                    parent_proposal_id=proposal.id,
                    editor_id=str(body.get("editorId") or "local-user"),
                    patch_json=patch_json,
                    patch_digest=submitted_digest,
                )
                session.add(human_patch)
                await session.flush()
        patch_digest = (
            human_patch.patch_digest
            if human_patch is not None
            else proposal.proposal_digest
        )
        patched = apply_workflow_patch(version.definition_json, patch)
    except HTTPException:
        raise
    except WorkflowPatchViolation as exc:
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_PATCH_REJECTED", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_PATCH_INVALID", "message": str(exc)},
        ) from exc

    fixtures, fixture_digest = await _load_replay_fixtures(session, source_run)
    if fixture_digest != proposal.fixture_digest:
        raise HTTPException(
            409,
            detail={
                "code": "PATCH_FIXTURE_MISMATCH",
                "message": "source Run fixture set 与 proposal 不匹配",
            },
        )
    evidence_id = str(uuid4())
    lineage = {
        "status": "active",
        "patchOfObservationId": observation.id,
        "proposalId": proposal.id,
        "proposalDigest": proposal.proposal_digest,
        "agentRunId": proposal.agent_run_id,
        "promptVersion": proposal.prompt_version,
        "patchDigest": patch_digest,
        "humanPatchId": human_patch.id if human_patch is not None else None,
        "replayEvidenceId": evidence_id,
        "fixtureDigest": fixture_digest,
        "baseVersionId": version_id,
    }
    patched["metadata"] = {
        **patched.get("metadata", {}),
        "revisionLineage": {
            key: value for key, value in lineage.items() if value is not None
        },
    }
    compiled = compiler.compile(patched)
    patched_definition = compiled.definition.model_dump(
        by_alias=True, mode="json"
    )
    base_digest = compiler.compile(version.definition_json).digest
    replay = await run_replay_proof(
        version.definition_json,
        patched_definition,
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        expected_error_code=observation.error_code,
        fixtures=fixtures,
    )
    observation.baseline_reproduced = bool(
        replay["baselineReproduced"]
        and observation.evidence_kind in PATCHABLE_EVIDENCE_KINDS
        and observation.evidence_kind not in TRANSPORT_EVIDENCE_KINDS
    )
    eligibility = observation_eligibility(observation)
    evidence = ReplayEvidence(
        id=evidence_id,
        observation_id=observation.id,
        baseline_snapshot_json=replay["baseline"],
        patched_snapshot_json=replay["patched"] or {},
        diff_json=replay["diff"],
        passed=replay["passed"],
        fixture_digest=fixture_digest,
        base_version_id=version_id,
        patch_of_observation_id=observation.id,
        base_definition_digest=base_digest,
        patch_digest=patch_digest,
        patched_definition_digest=compiled.digest,
        proposal_id=proposal.id,
        human_patch_id=human_patch.id if human_patch is not None else None,
    )
    session.add(evidence)
    if not eligibility["eligible"] or not replay["passed"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": (
                    "PATCH_NOT_ELIGIBLE"
                    if not eligibility["eligible"]
                    else "REPLAY_FAILED"
                ),
                "message": eligibility["reason"],
                "replayEvidenceId": evidence.id,
                "replay": replay,
            },
        )

    diagnostics = [item.model_dump() for item in compiled.diagnostics]
    workflow = await session.get(Workflow, version.workflow_id)
    if workflow is None:
        raise HTTPException(409, "WorkflowVersion 所属 Workflow 不存在")
    latest = await session.scalar(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == workflow.id)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    if latest is None or latest.id != version.id:
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_BASE_CONFLICT",
                "message": "只能从当前最新发布版本创建修订草稿",
            },
        )
    current_metadata = workflow.draft_metadata_json or {}
    has_pending_revision = current_metadata.get("status") in {
        "pending_revision",
        "dirty_after_replay",
        "publishing",
    }
    draft_matches_base = (
        compiler.compile(workflow.draft_json).digest == version.digest
    )
    if (
        has_pending_revision
        or workflow.draft_base_version_id not in {None, version.id}
        or not draft_matches_base
    ):
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_REVISION_CONFLICT",
                "message": "原 Workflow 已存在其它草稿修订，拒绝静默覆盖",
                "currentRevision": workflow.draft_revision,
            },
        )
    expected_revision = workflow.draft_revision
    draft_metadata = {
        "status": "pending_revision",
        "baseVersionId": version_id,
        "observationId": observation.id,
        "sourceRunId": source_run_id,
        "fixtureDigest": fixture_digest,
        "replayEvidenceId": evidence.id,
        "replayApprovedDraftDigest": compiled.digest,
        "proposalId": proposal.id,
        "proposalDigest": proposal.proposal_digest,
        "patchDigest": patch_digest,
        "humanPatchId": human_patch.id if human_patch is not None else None,
    }
    claimed = await session.execute(
        update(Workflow)
        .where(
            Workflow.id == workflow.id,
            Workflow.draft_revision == expected_revision,
            or_(
                Workflow.draft_base_version_id == version.id,
                Workflow.draft_base_version_id.is_(None),
            ),
        )
        .values(
            draft_revision=expected_revision + 1,
            draft_json=patched_definition,
            draft_digest=compiled.digest,
            draft_base_version_id=version.id,
            draft_metadata_json=draft_metadata,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_REVISION_CONFLICT",
                "message": "草稿已被并发修改",
            },
        )
    await session.commit()
    return {
        "draftWorkflowId": workflow.id,
        "draftRevision": expected_revision + 1,
        "draftDigest": compiled.digest,
        "baseVersionId": version_id,
        "expectedNextVersion": version.version + 1,
        "draftMetadata": draft_metadata,
        "definition": patched_definition,
        "diagnostics": diagnostics,
        "eligibility": eligibility,
        "replay": {
            "replayEvidenceId": evidence.id,
            "passed": replay["passed"],
            "baselineReproduced": observation.baseline_reproduced,
            "failedNodeStatus": replay["patched"]["failedNodeStatus"],
            "sideEffectsSimulated": replay["patched"]["sideEffectsSimulated"],
        },
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }


@router.post("/workflows/{workflow_id}/replay", status_code=201)
async def replay_dirty_revision_draft(
    workflow_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, "工作流不存在")
    metadata = dict(workflow.draft_metadata_json or {})
    if metadata.get("status") != "dirty_after_replay":
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_NOT_DIRTY_AFTER_REPLAY",
                "message": "只有 Replay 后已变化的修订草稿可以重新 Replay",
            },
        )
    proposal = await session.get(RevisionProposal, metadata.get("proposalId"))
    human_patch = await session.get(HumanPatch, metadata.get("humanPatchId"))
    observation = await session.get(
        FailureObservation, metadata.get("observationId")
    )
    base_version = await session.get(
        WorkflowVersion, metadata.get("baseVersionId")
    )
    source_run = await session.get(Run, metadata.get("sourceRunId"))
    if (
        proposal is None
        or human_patch is None
        or observation is None
        or base_version is None
        or source_run is None
        or proposal.observation_id != observation.id
        or proposal.base_version_id != base_version.id
        or source_run.workflow_version_id != base_version.id
    ):
        raise HTTPException(
            409,
            detail={
                "code": "REPLAY_LINEAGE_CORRUPT",
                "message": "dirty draft 的 proposal/Patch/base/source lineage 不完整",
            },
        )
    latest_version_id = await session.scalar(
        select(WorkflowVersion.id)
        .where(WorkflowVersion.workflow_id == workflow.id)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    if latest_version_id != base_version.id:
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_BASE_CONFLICT",
                "message": "修订草稿的 base version 已不是当前最新版本",
            },
        )
    fixtures, fixture_digest = await _load_replay_fixtures(
        session, source_run
    )
    if fixture_digest != metadata.get("fixtureDigest"):
        raise HTTPException(
            409,
            detail={
                "code": "PATCH_FIXTURE_MISMATCH",
                "message": "重新 Replay 的 fixture set 已变化",
            },
        )
    evidence_id = str(uuid4())
    draft = dict(workflow.draft_json)
    draft["metadata"] = {
        **draft.get("metadata", {}),
        "revisionLineage": {
            "status": "active",
            "patchOfObservationId": observation.id,
            "proposalId": proposal.id,
            "proposalDigest": proposal.proposal_digest,
            "agentRunId": proposal.agent_run_id,
            "promptVersion": proposal.prompt_version,
            "patchDigest": human_patch.patch_digest,
            "humanPatchId": human_patch.id,
            "replayEvidenceId": evidence_id,
            "fixtureDigest": fixture_digest,
            "baseVersionId": base_version.id,
        },
    }
    try:
        compiled = compiler.compile(draft)
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={
                "code": "WORKFLOW_VALIDATION_FAILED",
                "diagnostics": [
                    diagnostic.model_dump() for diagnostic in exc.diagnostics
                ],
            },
        ) from exc
    definition = compiled.definition.model_dump(by_alias=True, mode="json")
    replay = await run_replay_proof(
        base_version.definition_json,
        definition,
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        expected_error_code=observation.error_code,
        fixtures=fixtures,
    )
    evidence = ReplayEvidence(
        id=evidence_id,
        observation_id=observation.id,
        baseline_snapshot_json=replay["baseline"],
        patched_snapshot_json=replay["patched"] or {},
        diff_json=replay["diff"],
        passed=replay["passed"],
        fixture_digest=fixture_digest,
        base_version_id=base_version.id,
        patch_of_observation_id=observation.id,
        base_definition_digest=base_version.digest,
        patch_digest=human_patch.patch_digest,
        patched_definition_digest=compiled.digest,
        proposal_id=proposal.id,
        human_patch_id=human_patch.id,
    )
    session.add(evidence)
    if not replay["passed"]:
        await session.commit()
        raise HTTPException(
            422,
            detail={
                "code": "REPLAY_FAILED",
                "message": "当前 dirty draft 未通过 Replay",
                "replayEvidenceId": evidence.id,
                "replay": replay,
            },
        )
    expected_revision = workflow.draft_revision
    current_digest = (
        workflow.draft_digest or compiler.compile(workflow.draft_json).digest
    )
    approved_metadata = {
        **metadata,
        "status": "pending_revision",
        "replayEvidenceId": evidence.id,
        "replayApprovedDraftDigest": compiled.digest,
        "patchDigest": human_patch.patch_digest,
    }
    claimed = await session.execute(
        update(Workflow)
        .where(
            Workflow.id == workflow.id,
            Workflow.draft_revision == expected_revision,
            or_(
                Workflow.draft_digest == current_digest,
                Workflow.draft_digest.is_(None),
            ),
            func.json_extract(
                Workflow.draft_metadata_json, "$.status"
            )
            == "dirty_after_replay",
        )
        .values(
            draft_revision=expected_revision + 1,
            draft_json=definition,
            draft_digest=compiled.digest,
            draft_metadata_json=approved_metadata,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise HTTPException(
            409,
            detail={
                "code": "DRAFT_REVISION_CONFLICT",
                "message": "重新 Replay 时草稿已被并发修改",
            },
        )
    await session.commit()
    return {
        "draftWorkflowId": workflow.id,
        "draftRevision": expected_revision + 1,
        "draftDigest": compiled.digest,
        "draftMetadata": approved_metadata,
        "definition": definition,
        "replay": {
            "replayEvidenceId": evidence.id,
            "passed": True,
        },
        "publishable": True,
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
    fixtures, fixture_digest = await _load_replay_fixtures(session, source_run)
    replay = await run_replay_proof(
        snapshot.definition_json,
        compiled.definition.model_dump(by_alias=True, mode="json"),
        safe_inputs=redact(source_run.input_json) or {},
        fail_node_id=observation.node_id,
        expected_error_code=observation.error_code,
        fixtures=fixtures,
    )
    observation.baseline_reproduced = bool(
        replay["baselineReproduced"]
        and observation.evidence_kind in PATCHABLE_EVIDENCE_KINDS
        and observation.evidence_kind not in TRANSPORT_EVIDENCE_KINDS
    )
    evidence = ReplayEvidence(
        id=str(uuid4()),
        observation_id=observation.id,
        baseline_snapshot_json=replay["baseline"],
        patched_snapshot_json=replay["patched"] or {},
        diff_json=replay["diff"],
        passed=replay["passed"],
        fixture_digest=fixture_digest,
        base_version_id=plan_id,
        patch_of_observation_id=observation.id,
        base_definition_digest=compiler.compile(
            snapshot.definition_json
        ).digest,
        patch_digest=digest_json(patch.model_dump(by_alias=True)),
        patched_definition_digest=compiled.digest,
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
        "expiresAt": rfc3339(item.expires_at),
        "callBudget": item.call_budget_json,
    }


@router.post("/workflow-tools/search")
async def workflow_search(
    body: WorkflowSearchRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """L0：有界 top-k 能力卡片，不返回完整 Schema 或 DAG。"""

    try:
        call = await consume_session_call(
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
        await record_disclosure_response(session, call.id, response, level="L0")
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
        call = await consume_session_call(
            session, body.session_id, "describe", {"refDigest": digest_json(body.ref)}
        )
        await require_disclosed_ref(session, body.session_id, body.ref)
        result = await describe_workflow(session, body.ref)
        await disclose_refs(
            session,
            body.session_id,
            [
                body.ref,
                *[
                    item["fragmentRef"]
                    for item in result.get("fragments", [])
                    if item.get("fragmentRef")
                ],
            ],
            "L1",
        )
        response = {"level": "L1", "workflow": result}
        await record_disclosure_response(session, call.id, response, level="L1")
        await session.commit()
        return response
    except CatalogError as exc:
        await session.rollback()
        raise HTTPException(
            404,
            detail={"code": "WORKFLOW_REF_NOT_FOUND", "message": str(exc)},
        ) from exc


@router.post("/workflow-tools/resolve")
async def workflow_resolve_exact(
    body: WorkflowDescribeRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按调用方持有的精确版本 ref 解析 published/resolvable Manifest。"""

    try:
        call = await consume_session_call(
            session,
            body.session_id,
            "describe",
            {"exactRefDigest": digest_json(body.ref)},
        )
        result = await describe_workflow(session, body.ref)
        await disclose_refs(session, body.session_id, [body.ref], "L1")
        response = {"level": "L1", "workflow": result}
        await record_disclosure_response(
            session, call.id, response, level="L1"
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
        call = await consume_session_call(
            session, body.session_id, "describe", {"refDigest": digest_json(body.ref)}
        )
        await require_disclosed_ref(session, body.session_id, body.ref)
        response = {
            "level": "L2",
            "fragment": await describe_fragment(session, body.ref),
        }
        await disclose_refs(session, body.session_id, [body.ref], "L2")
        await record_disclosure_response(session, call.id, response, level="L2")
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
        call = await consume_session_call(
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
        await record_disclosure_response(session, call.id, response, level="L2")
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
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Execute an explicitly submitted selection.

    This is the manual API boundary, not an Agent output path. Agent-originated
    selections are source-verified in ``_unbound_conversation_message`` before
    they reach the execution service.
    """

    try:
        selection = WorkflowSelection.model_validate(
            body, context={"manual_execution": True}
        )
        result = await _execute_selection_service(
            session, selection, source_verified=True
        )
        await session.commit()
        return result
    except (CatalogError, PlanCompileError, WorkflowValidationError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(
            422,
            detail={
                "code": "SELECTION_EXECUTION_REJECTED",
                "action": body.get("action"),
                "diagnostics": [{"message": str(exc)}],
            },
        ) from exc


async def _execute_selection_service(
    session: AsyncSession,
    body: WorkflowSelection,
    *,
    turn_id: str | None = None,
    source_verified: bool = False,
) -> dict[str, Any]:
    if body.action in {"run_full", "run_fragment", "compose"} and not source_verified:
        prepared, _ = await _prepare_selection(session, body)
        if prepared.action == "ask_user":
            raise PlanCompileError(
                f"{body.action} 缺少必填 bindings: {prepared.missing_inputs}"
            )
        body = prepared
    for ref in body.refs:
        await require_disclosed_ref(session, body.session_id, ref)
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
    if body.action == "run_full":
        manifest = await resolve_workflow_manifest(session, body.refs[0])
        version = await session.get(WorkflowVersion, manifest.workflow_version_id)
        if version is None:
            raise CatalogError("WorkflowVersion 不存在")
        run = await orchestrator.create_run(
            session, version, body.bindings, turn_id=turn_id
        )
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
        run = await orchestrator.create_plan_run(
            session, snapshot, body.bindings, turn_id=turn_id
        )
        return {
            "action": body.action,
            "status": run.status,
            "runId": run.id,
            "planSnapshotId": snapshot.id,
        }
    assert body.plan is not None
    await require_plan_refs_disclosed(session, body.session_id, body.plan)
    snapshot = await create_plan_snapshot(session, body.plan)
    run = await orchestrator.create_plan_run(
        session, snapshot, body.bindings, turn_id=turn_id
    )
    return {
        "action": body.action,
        "status": run.status,
        "runId": run.id,
        "planSnapshotId": snapshot.id,
    }


def _plan_view(item: PlanSnapshot) -> dict[str, Any]:
    definition = item.definition_json.get("spec", {})
    nodes = definition.get("nodes", [])
    return {
        "id": item.id,
        "digest": item.digest,
        "status": item.status,
        "expiresAt": rfc3339(item.expires_at),
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
        call = await consume_session_call(
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
            call.id,
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


@router.post("/workflow-versions/{workflow_version_id}/runs", status_code=202)
async def create_workflow_version_run(
    workflow_version_id: str,
    body: RunCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    version = await session.get(WorkflowVersion, workflow_version_id)
    if version is None:
        raise HTTPException(404, "已发布 WorkflowVersion 不存在")
    try:
        run = await orchestrator.create_run(session, version, body.input)
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "INVALID_INPUT", "message": str(exc)},
        ) from exc
    await session.commit()
    return run_view(run)


@router.get("/runs")
async def list_runs(
    component_id: str | None = Query(default=None, alias="componentId"),
    workflow_version_id: str | None = Query(
        default=None, alias="workflowVersionId"
    ),
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    statement = select(Run)
    if component_id:
        statement = statement.where(Run.component_id == component_id)
    if workflow_version_id:
        statement = statement.where(
            Run.workflow_version_id == workflow_version_id
        )
    if status:
        statement = statement.where(Run.status == status)
    if cursor:
        cursor_at, cursor_id = _decode_run_cursor(cursor)
        statement = statement.where(
            or_(
                Run.created_at < cursor_at,
                (Run.created_at == cursor_at) & (Run.id < cursor_id),
            )
        )
    rows = (
        await session.scalars(
            statement.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for item in rows:
        latest_presentation = await session.scalar(
            select(RunPresentation)
            .where(RunPresentation.run_id == item.id)
            .order_by(RunPresentation.revision.desc())
            .limit(1)
        )
        view = run_view(item)
        view["presentation"] = (
            presentation_view(latest_presentation) if latest_presentation else None
        )
        items.append(view)
    return {
        "items": items,
        "nextCursor": (
            _run_cursor(rows[-1].created_at, rows[-1].id)
            if has_more and rows
            else None
        ),
    }


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
    observations = (
        await session.scalars(
            select(FailureObservation)
            .where(FailureObservation.last_run_id == run_id)
            .order_by(FailureObservation.created_at)
        )
    ).all()
    observation_ids = [item.id for item in observations]
    replay_evidence = (
        (
            await session.scalars(
                select(ReplayEvidence)
                .where(ReplayEvidence.observation_id.in_(observation_ids))
                .order_by(ReplayEvidence.created_at)
            )
        ).all()
        if observation_ids
        else []
    )
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
            "createdAt": rfc3339(item.created_at),
            "updatedAt": rfc3339(item.updated_at),
            "decidedAt": rfc3339(item.decided_at),
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
            "createdAt": rfc3339(item.created_at),
            "updatedAt": rfc3339(item.updated_at),
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
        "planExpiresAt": rfc3339(plan_snapshot.expires_at) if plan_snapshot else None,
        "sourceMap": plan_snapshot.source_map_json if plan_snapshot else {},
        "executionClass": validation.get("executionClass"),
        "capabilityLocks": validation.get("capabilityLocks", []),
        "mcpLocks": validation.get("mcpLocks", []),
        "modelRuntime": run.runtime_snapshot_json or {},
        "lineage": source_definition.get("metadata", {}).get("lineage", {}),
        "revisionLineage": (
            version.revision_lineage_json
            if version
            else source_definition.get("metadata", {}).get(
                "revisionLineage", {}
            )
        ),
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
            "createdAt": rfc3339(item.created_at),
            "updatedAt": rfc3339(item.updated_at),
            "leaseExpiresAt": rfc3339(item.lease_expires_at),
            "heartbeatAt": rfc3339(item.heartbeat_at),
        }
        for item in checkpoints
    ]
    evidence_by_observation: dict[str, list[ReplayEvidence]] = {}
    for evidence in replay_evidence:
        evidence_by_observation.setdefault(evidence.observation_id, []).append(
            evidence
        )
    result["failureObservations"] = [
        {
            "id": item.id,
            "fingerprint": item.fingerprint,
            "nodeId": item.node_id,
            "errorCode": item.error_code,
            "classification": item.classification,
            "occurrence": item.occurrence,
            "recovered": item.recovered,
            "baselineReproduced": item.baseline_reproduced,
            "evidenceKind": item.evidence_kind,
            "reproductionPolicy": item.reproduction_policy_json,
            "evidenceIds": item.evidence_ids_json,
            "checkpointId": item.last_checkpoint_id,
            "createdAt": rfc3339(item.created_at),
            "updatedAt": rfc3339(item.updated_at),
            "replayEvidence": [
                {
                    "id": evidence.id,
                    "passed": evidence.passed,
                    "fixtureDigest": evidence.fixture_digest,
                    "baseVersionId": evidence.base_version_id,
                    "patchOfObservationId": evidence.patch_of_observation_id,
                    "baseDefinitionDigest": evidence.base_definition_digest,
                    "patchDigest": evidence.patch_digest,
                    "patchedDefinitionDigest": (
                        evidence.patched_definition_digest
                    ),
                    "proposalId": evidence.proposal_id,
                    "humanPatchId": evidence.human_patch_id,
                    "createdAt": rfc3339(evidence.created_at),
                    "updatedAt": rfc3339(evidence.updated_at),
                }
                for evidence in evidence_by_observation.get(item.id, [])
            ],
        }
        for item in observations
    ]
    presentations = (
        await session.scalars(
            select(RunPresentation)
            .where(RunPresentation.run_id == run.id)
            .order_by(RunPresentation.revision)
        )
    ).all()
    result["presentations"] = [presentation_view(item) for item in presentations]
    result["presentation"] = (
        presentation_view(presentations[-1]) if presentations else None
    )
    result["presentationExpected"] = presentation_policy(version)["enabled"]
    usage_rows = (
        await session.scalars(
            select(ModelUsage)
            .where(ModelUsage.run_id == run.id)
            .order_by(ModelUsage.created_at)
        )
    ).all()
    result["modelUsage"] = [
        {
            "callId": item.call_id,
            "attempt": item.attempt,
            "stage": item.stage,
            "provider": item.provider,
            "model": item.model,
            "protocol": item.protocol,
            "runtimeType": item.runtime_type,
            "adapterVersion": item.adapter_version,
            "adapterDigest": item.adapter_digest,
            "runtimeUsed": item.runtime_used,
            "runtimeDigest": item.runtime_digest,
            "fallbackReason": item.fallback_reason,
            "errorCode": item.error_code,
            "inputTokens": item.input_tokens,
            "outputTokens": item.output_tokens,
            "totalTokens": item.total_tokens,
            "complete": item.usage_complete,
            "attempted": True,
            "dispatched": item.request_dispatched_at is not None,
            "completed": item.completed_at is not None,
            "attemptedAt": rfc3339(item.attempted_at),
            "requestDispatchedAt": rfc3339(item.request_dispatched_at),
            "completedAt": rfc3339(item.completed_at),
        }
        for item in usage_rows
    ]
    actual_runtimes = list(
        dict.fromkeys(
            item.runtime_used
            for item in usage_rows
            if item.runtime_used is not None
            and item.request_dispatched_at is not None
        )
    )
    runtime_snapshot = dict(run.runtime_snapshot_json or {})
    runtime_snapshot["runtimeUsed"] = (
        actual_runtimes[0]
        if len(actual_runtimes) == 1
        else "mixed"
        if actual_runtimes
        else None
    )
    runtime_snapshot["runtimeCalls"] = actual_runtimes
    runtime_snapshot["modelCalled"] = any(
        item.request_dispatched_at is not None for item in usage_rows
    )
    runtime_snapshot["totalTokens"] = sum(
        item.total_tokens or 0
        for item in usage_rows
        if item.request_dispatched_at is not None
    )
    result["runtimeSnapshot"] = runtime_snapshot
    result["dependencyVersions"]["modelRuntime"] = runtime_snapshot
    return result


@router.get("/runs/{run_id}/technical-error")
async def get_run_technical_error(
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return bounded technical evidence only after an explicit detail request."""

    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    nodes = (
        await session.scalars(
            select(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.error_json.is_not(None))
            .order_by(NodeRun.created_at, NodeRun.attempt)
        )
    ).all()
    projected = [
        {"scope": "run", "ref": f"run:{run.id}", "error": technical_error(run.error_json)},
        *[
            {
                "scope": "node",
                "ref": f"run:{run.id}:node:{item.id}",
                "nodeId": item.node_id,
                "attempt": item.attempt,
                "error": technical_error(item.error_json),
            }
            for item in nodes
        ],
    ]
    evidence: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for item in projected:
        error = item.pop("error")
        if error is None:
            continue
        detail = error.pop("technicalDetail", {})
        evidence_digest = digest_json(detail)
        bucket = evidence.setdefault(
            evidence_digest,
            {"digest": evidence_digest, "detail": detail, "refs": []},
        )
        bucket["refs"].append(item["ref"])
        errors.append({**item, **error, "evidenceDigest": evidence_digest})
    response = {
        "runId": run.id,
        "errors": errors,
        "evidence": list(evidence.values()),
    }
    max_bytes = 6000
    encoded = digest_safe_json(response).encode("utf-8")
    if len(encoded) > max_bytes:
        for item in response["evidence"]:
            summary = digest_safe_json(item["detail"])
            item["detail"] = {"summary": summary[:512], "truncated": True}
        encoded = digest_safe_json(response).encode("utf-8")
    if len(encoded) > max_bytes:
        response["evidence"] = [
            {
                "digest": item["digest"],
                "refs": item["refs"],
                "detail": {"summary": "技术证据已截断", "truncated": True},
            }
            for item in response["evidence"][:4]
        ]
        response["truncated"] = True
        while (
            len(digest_safe_json(response).encode("utf-8")) > max_bytes
            and response["errors"]
        ):
            response["errors"].pop()
    return response


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
    limit: int = Query(default=200, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    events = (
        await session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after)
            .order_by(RunEvent.seq)
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": item.id,
            "seq": item.seq,
            "type": item.type,
            "payload": item.payload_json,
            "createdAt": rfc3339(item.created_at),
        }
        for item in events
    ]


@router.get("/runs/{run_id}/events/stream")
async def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    async def generate() -> Any:
        cursor = max(after, int(last_event_id or 0))
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
                presentation_settled = True
                if run and run.status == "succeeded" and run.workflow_version_id:
                    version = await session.get(
                        WorkflowVersion, run.workflow_version_id
                    )
                    if presentation_policy(version)["enabled"]:
                        presentation = await session.scalar(
                            select(RunPresentation)
                            .where(RunPresentation.run_id == run.id)
                            .order_by(RunPresentation.revision.desc())
                            .limit(1)
                        )
                        presentation_settled = bool(
                            presentation
                            and presentation.status in {"completed", "failed"}
                        )
            for item in events:
                cursor = item.seq
                data = {
                    "runId": run_id,
                    "seq": item.seq,
                    "type": item.type,
                    "payload": item.payload_json,
                    "createdAt": rfc3339(item.created_at),
                }
                yield (
                    f"id: {item.seq}\nevent: run.event\n"
                    f"data: {digest_safe_json(data)}\n\n"
                )
            if (
                run
                and run.status in {"succeeded", "failed", "cancelled"}
                and presentation_settled
                and not events
            ):
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
    if artifact.run_id is not None:
        source_root = root / artifact.run_id
    elif artifact.conversation_run_id is not None:
        source_root = root / "conversation-agents" / artifact.conversation_run_id
    else:
        raise HTTPException(410, "Artifact 来源不可用")
    path = (source_root / storage_key).resolve()
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
        workflow_mode="enabled",
    )
    session.add(item)
    await session.commit()
    return {
        "id": item.id,
        "title": item.title,
        "boundComponentId": item.bound_component_id,
        "workflowMode": item.workflow_mode,
    }


def _conversation_cursor(last_message_at: datetime | None, conversation_id: str) -> str:
    payload = json.dumps(
        {
            "at": rfc3339(last_message_at),
            "id": conversation_id,
        },
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_conversation_cursor(cursor: str) -> tuple[datetime | None, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return (
            datetime.fromisoformat(payload["at"]) if payload.get("at") else None,
            str(payload["id"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "无效的 conversation cursor") from exc


def _run_cursor(created_at: datetime, run_id: str) -> str:
    return _conversation_cursor(created_at, run_id)


def _decode_run_cursor(cursor: str) -> tuple[datetime, str]:
    created_at, run_id = _decode_conversation_cursor(cursor)
    if created_at is None:
        raise HTTPException(422, "run cursor 缺少时间")
    return created_at, run_id


@router.get("/conversations")
async def list_conversations(
    cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    query: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    statement = select(Conversation)
    if query:
        statement = statement.where(Conversation.title.contains(query.strip()))
    if cursor:
        cursor_at, cursor_id = _decode_conversation_cursor(cursor)
        if cursor_at is None:
            statement = statement.where(
                Conversation.last_message_at.is_(None),
                Conversation.id < cursor_id,
            )
        else:
            statement = statement.where(
                or_(
                    Conversation.last_message_at < cursor_at,
                    Conversation.last_message_at.is_(None),
                    (
                        (Conversation.last_message_at == cursor_at)
                        & (Conversation.id < cursor_id)
                    ),
                )
            )
    rows = (
        await session.scalars(
            statement.order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.id.desc(),
            ).limit(limit + 1)
        )
    ).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for item in rows:
        latest = await session.scalar(
            select(Message)
            .where(Message.conversation_id == item.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        active_turn = await session.scalar(
            select(ConversationTurn.id).where(
                ConversationTurn.conversation_id == item.id,
                ConversationTurn.completed_at.is_(None),
            )
        )
        content = latest.content_json if latest else {}
        items.append(
            {
                "id": item.id,
                "title": item.title,
                "preview": str(content.get("text", ""))[:120],
                "lastMessageAt": (
                    rfc3339(item.last_message_at)
                ),
                "status": "active" if active_turn else "idle",
                "workflowMode": item.workflow_mode,
            }
        )
    return {
        "items": items,
        "nextCursor": (
            _conversation_cursor(rows[-1].last_message_at, rows[-1].id)
            if has_more and rows
            else None
        ),
    }


@router.patch("/conversations/{conversation_id}/workflow-mode")
async def update_conversation_workflow_mode(
    conversation_id: str,
    body: ConversationModeUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    item = await session.get(Conversation, conversation_id)
    if item is None:
        raise HTTPException(404, "对话不存在")
    previous = item.workflow_mode
    item.workflow_mode = body.workflow_mode
    cancelled_pending = 0
    if previous == "enabled" and body.workflow_mode == "disabled":
        result = await session.execute(
            update(PendingSelection)
            .where(
                PendingSelection.conversation_id == item.id,
                PendingSelection.status == "awaiting_input",
            )
            .values(status="cancelled_by_mode_change")
        )
        cancelled_pending = result.rowcount
    await session.commit()
    return {
        "id": item.id,
        "workflowMode": item.workflow_mode,
        "appliesFrom": "next_turn",
        "cancelledPendingSelections": cancelled_pending,
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
    turns = (
        await session.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id == item.id)
            .order_by(ConversationTurn.accepted_at)
        )
    ).all()
    return {
        "id": item.id,
        "title": item.title,
        "boundComponentId": item.bound_component_id,
        "workflowMode": item.workflow_mode,
        "lastMessageAt": (
            rfc3339(item.last_message_at)
        ),
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content_json,
                "turnId": message.turn_id,
                "createdAt": rfc3339(message.created_at),
            }
            for message in messages
        ],
        "turns": [await turn_view(session, turn) for turn in turns],
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
    accepted_at = utcnow()
    turn = ConversationTurn(
        id=str(uuid4()),
        conversation_id=conversation.id,
        workflow_mode_snapshot=conversation.workflow_mode,
        status="processing",
        accepted_at=accepted_at,
    )
    session.add(turn)
    await session.flush()
    user_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=turn.id,
        role="user",
        content_json={"type": "text", "text": body.content},
        created_at=accepted_at,
    )
    session.add(user_message)
    await session.flush()
    turn.user_message_id = user_message.id
    component_id = (
        body.component_id or conversation.bound_component_id
        if conversation.workflow_mode == "enabled"
        else None
    )
    assistant_content: dict[str, Any]
    conversation_run: ConversationRun | None = None
    if component_id:
        # 绑定组件或显式指定组件：补参齐全则启动 Workflow，否则请求补参。
        assistant_content = await _bound_component_message(
            session, component_id, body.content, body.inputs, turn.id
        )
    elif conversation.workflow_mode == "disabled":
        config = AgentRunConfig.default()
        config.allowed_capabilities = tuple(
            capability
            for capability in config.allowed_capabilities
            if not capability.startswith("workflow.")
        )
        conversation_run = await enqueue_agent_run(
            session,
            conversation,
            body.content,
            config=config,
            turn_id=turn.id,
        )
        assistant_content = {
            **build_agent_run_message(
                conversation_run,
                "本轮已冻结为工作流关闭模式，由通用 Agent 处理。",
            ),
            "workflowModeSnapshot": "disabled",
        }
    else:
        assistant_content, conversation_run = await _unbound_conversation_message(
            session, conversation, body, turn.id, user_message.id
        )
    responded_at = utcnow()
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=turn.id,
        role="assistant",
        content_json=assistant_content,
        created_at=responded_at,
    )
    turn.first_response_at = responded_at
    assistant_content["workflowModeSnapshot"] = turn.workflow_mode_snapshot
    if assistant_content.get("state") == "ask_user":
        turn.status = "ask_user"
        turn.completed_at = responded_at
        turn.elapsed_ms = max(
            0, int((responded_at - accepted_at).total_seconds() * 1000)
        )
    elif assistant_content.get("state") in {"error", "unavailable"}:
        turn.status = "error"
        turn.completed_at = responded_at
        turn.elapsed_ms = max(
            0, int((responded_at - accepted_at).total_seconds() * 1000)
        )
    session.add_all(
        [
            assistant_message,
            *([conversation_run] if conversation_run is not None else []),
        ]
    )
    await session.flush()
    turn.assistant_message_id = assistant_message.id
    conversation.last_message_at = responded_at
    conversation.updated_at = responded_at
    if conversation.title == "新对话":
        title = " ".join(body.content.split())
        conversation.title = title[:32] + ("…" if len(title) > 32 else "")
    await session.commit()
    return {
        "userMessageId": user_message.id,
        "assistant": assistant_message.content_json,
        "turn": await turn_view(session, turn),
    }


async def _unbound_conversation_message(
    session: AsyncSession,
    conversation: Conversation,
    body: MessageCreate,
    turn_id: str,
    user_message_id: str,
) -> tuple[dict[str, Any], ConversationRun | None]:
    provider = await provider_readiness()
    if not semantic_workflow_selection_available(
        probe_verified=bool(provider.get("modelAvailable"))
    ):
        return (
            {
                "type": "workflow_selection",
                "state": "unavailable",
                "code": "WORKFLOW_SELECTION_UNAVAILABLE",
                "text": (
                    "自动选择不可用：未配置真实语义 Agent Runtime。"
                    "可关闭工作流后继续使用通用 Agent；协议 fixture 不会用于产品语义选择。"
                ),
                "offerAgentRun": True,
                "selectionMode": "unavailable",
            },
            None,
        )
    pending = await session.scalar(
        select(PendingSelection)
        .where(
            PendingSelection.conversation_id == conversation.id,
            PendingSelection.status == "awaiting_input",
            PendingSelection.expires_at > utcnow(),
        )
        .order_by(PendingSelection.created_at.desc())
        .limit(1)
    )
    if pending is not None:
        supplied = dict(body.inputs or {})
        missing = list(pending.missing_inputs_json or [])
        if not supplied and len(missing) == 1:
            compact_answer = body.content.strip()
            if (
                compact_answer
                and len(compact_answer) <= 64
                and not any(mark in compact_answer for mark in "，。！？；\n")
            ):
                supplied[missing[0]] = compact_answer
        bindings = {**(pending.bindings_json or {}), **supplied}
        stored = dict(pending.selection_json)
        resume_action = stored.pop("resumeAction", "run_full")
        trusted_tool_evidence = stored.pop("_verifiedToolEvidence", {})
        selection = WorkflowSelection.model_validate(
            {
                **stored,
                "action": resume_action,
                "bindings": bindings,
                "bindingSources": [
                    *[
                        {
                            **item,
                            "source": (
                                "history_message"
                                if item.get("source") == "user_message"
                                else item.get("source")
                            ),
                        }
                        for item in stored.get("bindingSources", [])
                    ],
                    *[
                        {
                            "field": field,
                            "source": "user_message",
                            "messageId": user_message_id,
                            "quote": str(value),
                            "confidence": 1,
                            "uncertain": False,
                        }
                        for field, value in supplied.items()
                        if str(value) in body.content
                    ],
                ],
                "missingInputs": [],
                "question": None,
            }
        )
        selection, _ = await _prepare_selection(
            session,
            selection,
            conversation_id=conversation.id,
            current_message_id=user_message_id,
            trusted_tool_evidence=trusted_tool_evidence,
        )
        if selection.action == "ask_user":
            pending.bindings_json = bindings
            pending.missing_inputs_json = selection.missing_inputs
            return (
                {
                    "type": "workflow_selection",
                    "state": "ask_user",
                    "text": selection.question,
                    "required": selection.missing_inputs,
                    "pendingSelectionId": pending.id,
                    "disclosureSessionId": pending.disclosure_session_id,
                    "agentAction": "ask_user",
                },
                None,
            )
        result = await _execute_selection_service(
            session, selection, turn_id=turn_id, source_verified=True
        )
        pending.status = "resumed"
        pending.bindings_json = bindings
        return _selection_result_message(selection, result, resumed=True), None

    disclosure = await create_disclosure_session(
        session,
        conversation_id=conversation.id,
        agent_run_id=f"selection:{uuid4()}",
    )
    selection, tool_timeline, tool_evidence = await _select_workflow_candidate(
        session,
        body.content,
        disclosure.id,
        turn_id=turn_id,
        user_message_id=user_message_id,
    )
    selection, resume_action = await _prepare_selection(
        session,
        selection,
        conversation_id=conversation.id,
        current_message_id=user_message_id,
        tool_evidence=tool_evidence,
    )
    if selection.action == "ask_user":
        pending = PendingSelection(
            id=str(uuid4()),
            conversation_id=conversation.id,
            disclosure_session_id=disclosure.id,
            original_task=body.content,
            selection_json={
                **selection.model_dump(by_alias=True),
                "resumeAction": resume_action,
                "_verifiedToolEvidence": tool_evidence,
            },
            bindings_json=selection.bindings,
            missing_inputs_json=selection.missing_inputs,
            status="awaiting_input",
            expires_at=disclosure.expires_at,
        )
        session.add(pending)
        return (
            {
                "type": "workflow_selection",
                "state": "ask_user",
                "text": selection.question,
                "required": selection.missing_inputs,
                "refs": selection.refs,
                "pendingSelectionId": pending.id,
                "agentAction": selection.action,
                "selectionReason": selection.reason,
                "disclosureSessionId": disclosure.id,
                "disclosureLevel": "L1",
                "toolTimeline": tool_timeline,
                "bindingSources": [
                    item.model_dump(by_alias=True) for item in selection.binding_sources
                ],
            },
            None,
        )
    if selection.action == "continue_agent":
        used_steps = sum(
            item.get("type") == "tool.requested" for item in tool_timeline
        )
        config = AgentRunConfig.default()
        config.allowed_capabilities = _resolve_agent_capabilities(
            selection.required_capabilities or None
        )
        config.max_steps = max(1, config.max_steps - used_steps)
        continuation_context = [
            {
                "callId": item.get("callId"),
                "capabilityRef": item.get("capabilityRef"),
                "level": item.get("level"),
                "status": item.get("status"),
                "diagnostics": item.get("diagnostics", []),
                "result": item.get("result"),
                "resultDigest": item.get("resultDigest"),
            }
            for item in tool_timeline
            if item.get("type") == "tool.result"
        ]
        remaining_budget = {
            "steps": config.max_steps,
            "totalTokens": config.max_total_tokens,
            "disclosureCallsUsed": used_steps,
        }
        run = await enqueue_agent_run(
            session,
            conversation,
            body.content,
            config=config,
            prompt=(
                "继续原始用户任务。Workflow 发现阶段已完成，禁止把用户回答重新当作"
                " Catalog 查询；保留以下已完成 ToolResult 摘要并在剩余预算内继续："
                f"{json.dumps(continuation_context, ensure_ascii=False)}\n"
                f"remainingBudget={json.dumps(remaining_budget)}\n"
                f"用户任务：{body.content}"
            ),
            turn_id=turn_id,
        )
        run.input_json = {
            **run.input_json,
            "disclosureContext": continuation_context,
            "remainingBudget": remaining_budget,
        }
        content = build_agent_run_message(
            run,
            "目录中没有适合的已发布能力，已由通用 Agent 继续处理。",
        )
        return (
            {
                **content,
                "agentAction": selection.action,
                "selectionReason": selection.reason,
                "disclosureSessionId": disclosure.id,
                "toolTimeline": tool_timeline,
            },
            run,
        )
    result = await _execute_selection_service(
        session, selection, turn_id=turn_id, source_verified=True
    )
    message = _selection_result_message(selection, result, resumed=False)
    message["toolTimeline"] = tool_timeline
    return message, None


def _selection_result_message(
    selection: WorkflowSelection,
    result: dict[str, Any],
    *,
    resumed: bool,
) -> dict[str, Any]:
    return {
        "type": "workflow_run",
        "state": "workflow_run",
        "runId": result["runId"],
        **(
            {"workflowVersionId": result["workflowVersionId"]}
            if result.get("workflowVersionId")
            else {"planSnapshotId": result.get("planSnapshotId")}
        ),
        "refs": selection.refs,
        "agentAction": selection.action,
        "selectionReason": selection.reason,
        "bindingSources": [
            item.model_dump(by_alias=True) for item in selection.binding_sources
        ],
        "disclosureSessionId": selection.session_id,
        "disclosureLevel": "L2" if selection.action != "run_full" else "L1",
        "resumedPendingSelection": resumed,
        "text": "选择已执行，后续由 Workflow Runtime 接管。",
    }


async def _select_workflow_candidate(
    session: AsyncSession,
    user_text: str,
    session_id: str,
    *,
    turn_id: str | None = None,
    user_message_id: str | None = None,
) -> tuple[
    WorkflowSelection,
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    if (
        not agent_runtime_config().configured
    ):
        raise HTTPException(
            503,
            detail={
                "code": "WORKFLOW_SELECTION_UNAVAILABLE",
                "message": "语义选择服务不可用",
            },
        )
    selection_schema = WorkflowSelection.model_json_schema(by_alias=True)
    selection_schema["properties"].pop("sessionId", None)
    selection_schema["required"] = [
        item for item in selection_schema.get("required", []) if item != "sessionId"
    ]
    settings = get_settings()
    request = AgentHostRequest(
        run_id=f"workflow-selection-{uuid4()}",
        prompt=(
            "Progressive WorkflowSelection。你必须先调用 workflow.catalog(action=search) "
            "获取 L0，只在需要时通过同一窄工具请求 describe/describe_fragment/slice/"
            "compile_plan。"
            "不得选择未披露 ref，不得假设 Schema/DAG；编译失败时根据 diagnostics 修正。"
            "比较 useWhen/doNotUseWhen、输入输出、副作用和成本后输出 WorkflowSelection。"
            "从用户明确表达提取 bindings，并为每个字段返回 bindingSources（原文 quote、"
            "messageId、confidence、uncertain）；禁止编造。只有 Schema 校验后仍缺失或"
            "有真实歧义才 ask_user，无合适候选时 continue_agent。\n"
            f"userMessageId={user_message_id or 'unknown'}\n"
            f"user={body_text(user_text)}\n"
        ),
        output_schema=selection_schema,
        working_directory=get_settings().artifact_root / "workflow-selections",
        requested_capabilities=("workflow.catalog",),
        max_steps=6,
        max_total_tokens=settings.agent_max_total_tokens,
        max_input_tokens=settings.agent_max_input_tokens,
        max_output_tokens=settings.agent_max_output_tokens,
        max_billable_tokens=settings.agent_max_billable_tokens,
        max_duration_seconds=60,
    )
    result = None
    try:
        registry = ToolAdapterRegistry()
        registry.register(workflow_catalog_adapter(session, session_id))
        result = await AgentHost(tool_registry=registry).execute(request)
        try:
            selection = WorkflowSelection.model_validate(
                {**result.output, "sessionId": session_id}
            )
        except ValidationError:
            raw_bindings = result.output.get("bindings")
            if not isinstance(raw_bindings, dict) or not raw_bindings:
                raise
            raw_plan = result.output.get("plan")
            raw_refs = result.output.get("refs")
            selection = WorkflowSelection.model_validate(
                {
                    "action": "ask_user",
                    "reason": "Agent 返回的参数来源结构无效",
                    "refs": (
                        raw_refs
                        if isinstance(raw_plan, dict) and isinstance(raw_refs, list)
                        else raw_refs[:1]
                        if isinstance(raw_refs, list)
                        else []
                    ),
                    "bindings": {},
                    "bindingSources": [],
                    "missingInputs": list(raw_bindings),
                    "question": (
                        "参数来源无法由服务端验证，请重新提供："
                        + ", ".join(raw_bindings)
                    ),
                    "plan": raw_plan if isinstance(raw_plan, dict) else None,
                    "sessionId": session_id,
                }
            )
        if turn_id:
            runtime = agent_runtime_config()
            await record_model_usage(
                session,
                call_id=request.run_id,
                stage="workflow_selection",
                turn_id=turn_id,
                provider=result.provider or runtime.provider,
                model=result.model,
                protocol=runtime.protocol,
                runtime_type=runtime.type,
                adapter_version=runtime.adapter_version,
                adapter_digest=runtime.adapter_digest,
                runtime_used="agent_runtime",
                runtime_digest=runtime.digest,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens or None,
                usage_complete=result.usage_complete,
                dispatched=result.request_dispatched,
                completed=True,
            )
    except (AgentHostError, ValueError) as exc:
        runtime = agent_runtime_config()
        dispatched = (
            result.request_dispatched
            if result is not None
            else bool(getattr(exc, "request_dispatched", False))
        )
        input_tokens = (
            result.input_tokens
            if result is not None
            else int(getattr(exc, "input_tokens", 0))
        )
        output_tokens = (
            result.output_tokens
            if result is not None
            else int(getattr(exc, "output_tokens", 0))
        )
        total_tokens = (
            result.total_tokens
            if result is not None
            else int(getattr(exc, "total_tokens", 0))
        )
        usage_complete = (
            result.usage_complete
            if result is not None
            else bool(getattr(exc, "usage_complete", False))
        )
        error_code = (
            exc.code
            if isinstance(exc, AgentHostError)
            else "WORKFLOW_SELECTION_INVALID"
        )
        await record_model_usage(
            session,
            call_id=request.run_id,
            stage="workflow_selection",
            turn_id=turn_id,
            provider=runtime.provider,
            model=runtime.model,
            protocol=runtime.protocol,
            runtime_type=runtime.type,
            adapter_version=runtime.adapter_version,
            adapter_digest=runtime.adapter_digest,
            runtime_used="agent_runtime",
            runtime_digest=runtime.digest,
            error_code=error_code,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
            total_tokens=total_tokens or None,
            usage_complete=usage_complete,
            dispatched=dispatched,
            completed=False,
        )
        await finalize_turn(session, turn_id, "error")
        await session.commit()
        raise HTTPException(
            502,
            detail={"code": "WORKFLOW_SELECTION_FAILED", "message": str(exc)},
        ) from exc
    timeline = []
    tool_evidence: dict[str, dict[str, Any]] = {}
    level_by_capability = {
        "workflow.catalog": None,
        "workflow.search": "L0",
        "workflow.describe": "L1",
        "workflow.describe_fragment": "L2",
        "workflow.slice": "L2",
        "workflow.compile_plan": "L2",
    }
    for event in result.runtime_events:
        if event["type"] not in {"tool.requested", "tool.result"}:
            continue
        payload = event["payload"]
        capability = payload.get("capabilityRef")
        if capability not in level_by_capability:
            continue
        output = payload.get("output")
        call_id = payload.get("callId")
        if (
            event["type"] == "tool.result"
            and isinstance(call_id, str)
            and payload.get("status") == "succeeded"
            and isinstance(output, dict)
        ):
            tool_evidence[call_id] = output
        concrete_capability = capability
        if capability == "workflow.catalog":
            action = (
                output.get("action")
                if isinstance(output, dict)
                else (payload.get("arguments") or {}).get("action")
            )
            concrete_capability = {
                "search": "workflow.search",
                "describe": "workflow.describe",
                "describe_fragment": "workflow.describe_fragment",
                "slice": "workflow.slice",
                "compile_plan": "workflow.compile_plan",
            }.get(str(action), capability)
        bounded_result = _bounded_disclosure_result(concrete_capability, output)
        level = (
            output.get("level")
            if isinstance(output, dict) and output.get("level")
            else level_by_capability.get(concrete_capability)
        )
        timeline.append(
            {
                "type": event["type"],
                "callId": payload.get("callId"),
                "capabilityRef": concrete_capability,
                "level": level,
                "status": payload.get("status", "pending"),
                **(
                    {
                        "result": bounded_result,
                        "resultDigest": digest_json(bounded_result),
                    }
                    if bounded_result is not None
                    else {}
                ),
                **(
                    {"diagnostics": output.get("diagnostics", [])}
                    if isinstance(output, dict) and output.get("diagnostics")
                    else {}
                ),
            }
        )
    return selection, timeline, tool_evidence


def _bounded_disclosure_result(
    capability: str | None, output: Any
) -> dict[str, Any] | None:
    """保留 continuation 推理所需的 L0/L1 摘要，不复制完整 Manifest/DAG。"""

    if not isinstance(output, dict):
        return None
    if capability == "workflow.search":
        return {
            "level": "L0",
            "count": output.get("count", 0),
            "candidates": [
                {
                    key: item.get(key)
                    for key in (
                        "workflowRef",
                        "name",
                        "summary",
                        "tags",
                        "matchReason",
                    )
                }
                for item in output.get("items", [])[:8]
                if isinstance(item, dict)
            ],
        }
    if capability == "workflow.describe":
        workflow = output.get("workflow")
        if not isinstance(workflow, dict):
            return None
        return {
            "level": "L1",
            "workflowRef": workflow.get("workflowRef"),
            "summary": workflow.get("summary"),
            "useWhen": workflow.get("useWhen", [])[:8],
            "doNotUseWhen": workflow.get("doNotUseWhen", [])[:8],
            "examples": workflow.get("examples", [])[:6],
            "tags": workflow.get("tags", [])[:12],
            "inputSynopsis": workflow.get("inputSynopsis"),
            "outputSynopsis": workflow.get("outputSynopsis"),
            "fragments": [
                {
                    "fragmentRef": item.get("fragmentRef"),
                    "name": item.get("name"),
                    "summary": item.get("summary"),
                }
                for item in workflow.get("fragments", [])[:8]
                if isinstance(item, dict)
            ],
        }
    if capability in {
        "workflow.describe_fragment",
        "workflow.slice",
        "workflow.compile_plan",
    }:
        return {
            "level": output.get("level", "L2"),
            "compiled": output.get("compiled"),
            "diagnostics": output.get("diagnostics", [])[:8],
            "digest": output.get("digest"),
        }
    return None


def body_text(value: str) -> str:
    """限制进入选择 prompt 的用户文本长度。"""

    return value[:2000]


def _selection_resume_action(selection: WorkflowSelection) -> str:
    if selection.action != "ask_user":
        return selection.action
    if selection.plan is not None:
        return "compose"
    if selection.refs and selection.refs[0].startswith(
        ("fragment:", "fragment-snapshot:")
    ):
        return "run_fragment"
    return "run_full"


def _binding_normalization(value: Any, quote: str) -> str | None:
    if isinstance(value, str):
        if value == quote:
            return "exact"
        if value.strip() == quote.strip():
            return "whitespace"
        if quote.endswith(("市", "省", "区", "县")) and value == quote[:-1]:
            return "place_suffix_removed"
        return None
    if isinstance(value, bool):
        return "boolean" if quote.strip().lower() == str(value).lower() else None
    if isinstance(value, (int, float)):
        match = re.fullmatch(r"\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)\s*", quote)
        if match and float(quote) == float(value):
            return "number"
        return None
    if isinstance(value, list):
        try:
            decoded = json.loads(quote)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list) and decoded == value:
            return "json_array_v1"
        if value:
            parts = [
                item.strip()
                for item in re.split(r"\s*(?:,|，|、|;|；|\||和)\s*", quote.strip())
            ]
            if len(parts) == len(value) and all(parts):
                rebuilt: list[Any] = []
                for expected, part in zip(value, parts, strict=True):
                    if isinstance(expected, bool):
                        normalized = part.lower()
                        if normalized not in {"true", "false"}:
                            break
                        rebuilt.append(normalized == "true")
                    elif isinstance(expected, int):
                        if re.fullmatch(r"[-+]?\d+", part) is None:
                            break
                        rebuilt.append(int(part))
                    elif isinstance(expected, float):
                        if re.fullmatch(
                            r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
                            part,
                        ) is None:
                            break
                        rebuilt.append(float(part))
                    elif isinstance(expected, str):
                        rebuilt.append(part)
                    else:
                        break
                if rebuilt == value:
                    return "delimited_array_v1"
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "json" if canonical == quote.strip() else None


def _tool_evidence_value(output: dict[str, Any], path: str) -> Any:
    normalized = path.removeprefix("$.").removeprefix("output.")
    value: Any = output
    for part in normalized.split("."):
        if not part:
            continue
        match = re.fullmatch(r"([^\[]+)(?:\[(\d+)])?", part)
        if match is None or not isinstance(value, dict):
            raise KeyError(path)
        value = value[match.group(1)]
        if match.group(2) is not None:
            if not isinstance(value, list):
                raise KeyError(path)
            value = value[int(match.group(2))]
    return value


async def _verify_binding_sources(
    session: AsyncSession,
    selection: WorkflowSelection,
    *,
    conversation_id: str | None,
    current_message_id: str | None,
    tool_evidence: dict[str, dict[str, Any]],
) -> tuple[WorkflowSelection, list[str]]:
    sources_by_field: dict[str, list[Any]] = {}
    for source in selection.binding_sources:
        sources_by_field.setdefault(source.field, []).append(source)
    invalid = [
        field
        for field in set(selection.bindings) | set(sources_by_field)
        if field not in selection.bindings or len(sources_by_field.get(field, [])) != 1
    ]
    verified_sources = []
    for field, value in selection.bindings.items():
        matches = sources_by_field.get(field, [])
        if len(matches) != 1:
            continue
        source = matches[0]
        normalization: str | None = None
        if source.uncertain or source.confidence < 0.7 or not source.quote:
            invalid.append(field)
            continue
        if source.source in {"user_message", "history_message"}:
            message = (
                await session.get(Message, source.message_id)
                if source.message_id
                else None
            )
            if (
                message is None
                or conversation_id is None
                or message.conversation_id != conversation_id
                or message.role != "user"
                or (
                    source.source == "user_message"
                    and source.message_id != current_message_id
                )
            ):
                invalid.append(field)
                continue
            text_value = str((message.content_json or {}).get("text", ""))
            if source.quote not in text_value:
                invalid.append(field)
                continue
            normalization = _binding_normalization(value, source.quote)
        else:
            output = tool_evidence.get(source.call_id or "")
            try:
                evidence_value = (
                    _tool_evidence_value(output, source.path or "")
                    if output is not None and source.path
                    else None
                )
            except (KeyError, IndexError):
                evidence_value = None
            if evidence_value != value:
                invalid.append(field)
                continue
            encoded = (
                evidence_value
                if isinstance(evidence_value, str)
                else json.dumps(evidence_value, ensure_ascii=False, sort_keys=True)
            )
            if source.quote not in encoded:
                invalid.append(field)
                continue
            normalization = "tool_result_path"
        if normalization is None:
            invalid.append(field)
            continue
        verified_sources.append(
            source.model_copy(
                update={
                    "server_verified": True,
                    "normalization_type": normalization,
                    "uncertain": False,
                }
            )
        )
    selection = selection.model_copy(update={"binding_sources": verified_sources})
    return selection, list(dict.fromkeys(invalid))


async def _prepare_selection(
    session: AsyncSession,
    selection: WorkflowSelection,
    *,
    conversation_id: str | None = None,
    current_message_id: str | None = None,
    tool_evidence: dict[str, dict[str, Any]] | None = None,
    trusted_tool_evidence: dict[str, dict[str, Any]] | None = None,
) -> tuple[WorkflowSelection, str]:
    """Validate disclosure and turn missing executable inputs into resumable state."""

    resume_action = _selection_resume_action(selection)
    if selection.action == "continue_agent":
        return selection, resume_action
    if selection.action == "ask_user" and not selection.refs and selection.plan is None:
        return selection, resume_action

    required: list[str]
    schema: dict[str, Any]
    compiled_plan: dict[str, Any] | None = selection.compiled_plan
    effective_action = resume_action if selection.action == "ask_user" else selection.action
    if effective_action == "run_full":
        level = await require_disclosed_ref(
            session, selection.session_id, selection.refs[0]
        )
        if level not in {"L1", "L2"}:
            raise CatalogError("run_full 必须先披露 Workflow L1")
        manifest = await resolve_workflow_manifest(session, selection.refs[0])
        version = await session.get(WorkflowVersion, manifest.workflow_version_id)
        if version is None:
            raise CatalogError("WorkflowVersion 不存在")
        schema = version.definition_json["spec"]["inputSchema"]
        required = list(schema.get("required", []))
    elif effective_action == "run_fragment":
        level = await require_disclosed_ref(
            session, selection.session_id, selection.refs[0]
        )
        if level != "L2":
            raise CatalogError("run_fragment 必须先调用 workflow.describe_fragment 披露 L2")
        fragment = await describe_fragment(session, selection.refs[0])
        schema = fragment["boundaryInputSchema"]
        required = list(schema.get("required", []))
    else:
        assert selection.plan is not None
        await require_plan_refs_disclosed(
            session, selection.session_id, selection.plan
        )
        compiled_plan = await compile_plan(session, selection.plan)
        schema = selection.plan.get("inputSchema", {})
        required = list(schema.get("required", []))

    selection, source_invalid = await _verify_binding_sources(
        session,
        selection,
        conversation_id=conversation_id,
        current_message_id=current_message_id,
        tool_evidence={**(trusted_tool_evidence or {}), **(tool_evidence or {})},
    )
    missing = [field for field in required if field not in selection.bindings]
    validation_errors = sorted(
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).iter_errors(selection.bindings),
        key=lambda error: list(error.absolute_path),
    )
    invalid = list(source_invalid)
    for error in validation_errors:
        if error.validator == "required":
            continue
        if error.validator == "additionalProperties":
            extras = sorted(
                set(selection.bindings) - set(schema.get("properties", {}))
            )
            for field in extras:
                if field not in invalid:
                    invalid.append(field)
            continue
        field = str(next(iter(error.absolute_path), "__root__"))
        if field not in missing and field not in invalid:
            invalid.append(field)
    uncertain = [
        source.field
        for source in selection.binding_sources
        if source.uncertain and source.field not in missing
    ]
    unresolved = [*missing, *invalid, *[field for field in uncertain if field not in invalid]]
    if not unresolved:
        if compiled_plan is not selection.compiled_plan:
            selection = selection.model_copy(
                update={"compiled_plan": compiled_plan}
            )
        return selection, resume_action

    unresolved_set = set(unresolved)
    safe_bindings = {
        field: value
        for field, value in selection.bindings.items()
        if field not in unresolved_set
    }
    safe_sources = [
        source.model_dump(by_alias=True)
        for source in selection.binding_sources
        if source.field not in unresolved_set
    ]
    pending = WorkflowSelection.model_validate(
        {
            **selection.model_dump(by_alias=True),
            "action": "ask_user",
            "bindings": safe_bindings,
            "bindingSources": safe_sources,
            "missingInputs": unresolved,
            "question": (
                f"请补充输入：{', '.join(missing)}"
                if missing and not invalid and not uncertain
                else f"请确认或重新提供有效参数：{', '.join(unresolved)}"
            ),
            "compiledPlan": compiled_plan,
        }
    )
    return pending, resume_action


async def _bound_component_message(
    session: AsyncSession,
    component_id: str,
    content: str,
    inputs: dict[str, Any] | None,
    turn_id: str | None = None,
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
        run = await orchestrator.create_run(
            session, version, inputs or {}, component.id, turn_id=turn_id
        )
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
    if conversation.workflow_mode != "enabled":
        raise HTTPException(
            409,
            detail={
                "code": "WORKFLOW_MODE_DISABLED",
                "message": "该对话已关闭工作流，旧候选不能继续启动",
            },
        )
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
    source_turn = await session.scalar(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation.id)
        .order_by(ConversationTurn.accepted_at.desc())
        .limit(1)
    )
    try:
        run = await orchestrator.create_run(
            session,
            version,
            body.inputs,
            component.id,
            turn_id=source_turn.id if source_turn else None,
        )
    except ValueError as exc:
        raise HTTPException(
            422,
            detail={"code": "INVALID_INPUT", "message": str(exc)},
        ) from exc
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=source_turn.id if source_turn else None,
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
    now = utcnow()
    conversation.last_message_at = now
    conversation.updated_at = now
    if source_turn:
        source_turn.status = "processing"
        source_turn.completed_at = None
        source_turn.elapsed_ms = None
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
    requested_capabilities = _resolve_agent_capabilities(body.allowed_capabilities)
    if conversation.workflow_mode == "disabled":
        attempted_workflow = [
            item for item in requested_capabilities if item.startswith("workflow.")
        ]
        if body.allowed_capabilities is not None and attempted_workflow:
            raise HTTPException(
                403,
                detail={
                    "code": "WORKFLOW_MODE_DISABLED",
                    "message": "该对话当前禁止 workflow.* 能力",
                },
            )
        requested_capabilities = tuple(
            item
            for item in requested_capabilities
            if not item.startswith("workflow.")
        )
    source_turn = await session.scalar(
        select(ConversationTurn)
        .where(ConversationTurn.conversation_id == conversation.id)
        .order_by(ConversationTurn.accepted_at.desc())
        .limit(1)
    )
    config = AgentRunConfig(
        output_schema=default_config.output_schema,
        max_steps=min(body.max_steps or default_config.max_steps, default_config.max_steps),
        max_total_tokens=default_config.max_total_tokens,
        max_input_tokens=default_config.max_input_tokens,
        max_output_tokens=default_config.max_output_tokens,
        max_billable_tokens=default_config.max_billable_tokens,
        allowed_capabilities=requested_capabilities,
        max_duration_seconds=default_config.max_duration_seconds,
    )
    conversation_run = await enqueue_agent_run(
        session,
        conversation,
        body.content,
        config=config,
        turn_id=source_turn.id if source_turn else None,
    )
    assistant_content = build_agent_run_message(
        conversation_run,
        "通用 Agent 已进入队列，运行事件将实时更新。",
    )
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=source_turn.id if source_turn else None,
        role="assistant",
        content_json=assistant_content,
    )
    now = utcnow()
    conversation.last_message_at = now
    conversation.updated_at = now
    if source_turn:
        source_turn.status = "processing"
        source_turn.completed_at = None
        source_turn.elapsed_ms = None
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
            "createdAt": rfc3339(item.created_at),
        }
        for item in events
    ]


@router.get("/conversation-runs/{conversation_run_id}/artifacts")
async def list_conversation_run_artifacts(
    conversation_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if await session.get(ConversationRun, conversation_run_id) is None:
        raise HTTPException(404, "ConversationRun 不存在")
    artifacts = (
        await session.scalars(
            select(Artifact)
            .where(Artifact.conversation_run_id == conversation_run_id)
            .order_by(Artifact.created_at)
        )
    ).all()
    return [
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "uri": item.uri,
            "sha256": item.sha256,
            "bytes": item.size,
            "mediaType": item.media_type,
            "metadata": redact(item.metadata_json),
            "createdAt": rfc3339(item.created_at),
            "updatedAt": rfc3339(item.updated_at),
        }
        for item in artifacts
    ]


@router.get("/task-checkpoints/{checkpoint_id}")
async def get_task_checkpoint(
    checkpoint_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    checkpoint = await session.get(TaskCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise HTTPException(404, "TaskCheckpoint 不存在")
    snapshot = checkpoint.snapshot_json
    continuation_run_id = snapshot.get("continuationRunId")
    child_checkpoint = None
    if continuation_run_id:
        child_checkpoint = await session.scalar(
            select(TaskCheckpoint).where(
                TaskCheckpoint.source_kind == "conversation_run",
                TaskCheckpoint.source_id == continuation_run_id,
            )
        )
    child_checkpoint_id = snapshot.get("childCheckpointId") or (
        child_checkpoint.id if child_checkpoint is not None else None
    )
    lineage: list[dict[str, Any]] = []
    current = checkpoint
    visited: set[str] = set()
    while current is not None and current.id not in visited:
        visited.add(current.id)
        current_snapshot = current.snapshot_json or {}
        current_continuation_id = current_snapshot.get("continuationRunId")
        current_child = None
        if current_continuation_id:
            current_child = await session.scalar(
                select(TaskCheckpoint).where(
                    TaskCheckpoint.source_kind == "conversation_run",
                    TaskCheckpoint.source_id == current_continuation_id,
                )
            )
        current_child_id = current_snapshot.get("childCheckpointId") or (
            current_child.id if current_child is not None else None
        )
        lineage.append(
            {
                "checkpointId": current.id,
                "sourceRunId": current.source_id,
                "continuationRunId": current_continuation_id,
                "generation": current_snapshot.get(
                    "continuationGeneration", 0
                ),
                "status": current.status,
                "continuationStatus": current_snapshot.get(
                    "continuationStatus"
                ),
                "childCheckpointId": current_child_id,
            }
        )
        if current_child is None:
            break
        current = current_child
    leaf = current or checkpoint
    leaf_snapshot = leaf.snapshot_json or {}
    if leaf.status == "succeeded":
        chain_status = "succeeded"
        chain_error = None
    elif leaf.status == "awaiting_user":
        chain_status = "awaiting_user"
        chain_error = {
            "code": "CONTINUATION_AWAITING_USER",
            "message": leaf_snapshot.get("pendingQuestion")
            or "失败接力正在等待人工确认",
        }
    elif leaf.status == "terminal":
        chain_status = "failed"
        if leaf_snapshot.get("budgetExhausted"):
            chain_error = {
                "code": "CONTINUATION_BUDGET_EXHAUSTED",
                "message": "失败接力已耗尽 generation 预算",
            }
        elif (leaf_snapshot.get("remainingPlan") or {}).get("mode") == (
            "integrity_failed"
        ):
            chain_error = {
                "code": "CONTINUATION_INTEGRITY_FAILED",
                "message": (leaf_snapshot.get("remainingPlan") or {}).get(
                    "reason", "失败接力完整性校验失败"
                ),
            }
        else:
            chain_error = leaf_snapshot.get("continuationError") or {
                "code": "CONTINUATION_TERMINAL",
                "message": "失败接力已进入终态",
            }
    elif leaf.status == "failed" and leaf_snapshot.get("continuationRunId"):
        chain_status = (
            "cancelled"
            if leaf_snapshot.get("continuationStatus") == "cancelled"
            else "failed"
        )
        chain_error = leaf_snapshot.get("continuationError") or {
            "code": "CONTINUATION_FAILED",
            "message": "失败接力运行失败",
        }
    else:
        chain_status = "continuing"
        chain_error = None
    return {
        "id": checkpoint.id,
        "sourceKind": checkpoint.source_kind,
        "sourceId": checkpoint.source_id,
        "sourceDigest": checkpoint.source_digest,
        "status": checkpoint.status,
        "digest": checkpoint.digest,
        "createdAt": rfc3339(checkpoint.created_at),
        "updatedAt": rfc3339(checkpoint.updated_at),
        "leaseExpiresAt": rfc3339(checkpoint.lease_expires_at),
        "outcomeCertainty": snapshot.get("outcomeCertainty"),
        "pendingQuestion": snapshot.get("pendingQuestion"),
        "continuationRunId": continuation_run_id,
        "continuationStatus": snapshot.get("continuationStatus"),
        "continuationOutputDigest": snapshot.get(
            "continuationOutputDigest"
        ),
        "claimGeneration": checkpoint.claim_generation,
        "continuationGeneration": snapshot.get(
            "continuationGeneration", 0
        ),
        "sourceCheckpointId": snapshot.get("sourceCheckpointId"),
        "childCheckpointId": child_checkpoint_id,
        "nextContinuationRunId": (
            (child_checkpoint.snapshot_json or {}).get("continuationRunId")
            if child_checkpoint is not None
            else None
        ),
        "chainStatus": chain_status,
        "chainError": public_error(chain_error),
        "lineage": lineage,
        "planSnapshot": snapshot.get("planSnapshot"),
        "reportedError": public_error(snapshot.get("reportedError")),
        "effectiveError": public_error(snapshot.get("effectiveError")),
        "completedSteps": snapshot.get("completedSteps", []),
        "failedStep": snapshot.get("failedStep"),
        "stepStates": snapshot.get("stepStates", {}),
        "stepAttempts": snapshot.get("stepAttempts", {}),
        "stepStateDigest": snapshot.get("stepStateDigest"),
        "replanBudget": snapshot.get("replanBudget"),
        "budgetExhausted": snapshot.get("budgetExhausted"),
        "continuationError": public_error(snapshot.get("continuationError")),
        "completedActionLedger": snapshot.get("completedActionLedger", []),
        "failedAction": snapshot.get("failedAction"),
        "pendingToolCalls": snapshot.get("pendingToolCalls", []),
        "remainingPlan": snapshot.get("remainingPlan"),
    }


@router.post("/task-checkpoints/{checkpoint_id}/answer")
async def answer_task_checkpoint(
    checkpoint_id: str,
    body: TaskCheckpointAnswer,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    checkpoint = await session.get(TaskCheckpoint, checkpoint_id)
    if checkpoint is None:
        raise HTTPException(404, "TaskCheckpoint 不存在")
    if checkpoint.status != "awaiting_user":
        raise HTTPException(409, "TaskCheckpoint 不可回答或已消费")
    if (
        checkpoint.digest != body.snapshot_digest
        or digest_json(checkpoint.snapshot_json) != checkpoint.digest
    ):
        raise HTTPException(409, "TaskCheckpoint 快照已变化")
    next_status = (
        "pending" if body.answer.get("continue") is True else "cancelled"
    )
    next_snapshot = {
        **checkpoint.snapshot_json,
        "userAnswer": redact(body.answer),
    }
    answered = await session.execute(
        update(TaskCheckpoint)
        .where(
            TaskCheckpoint.id == checkpoint_id,
            TaskCheckpoint.status == "awaiting_user",
            TaskCheckpoint.digest == body.snapshot_digest,
        )
        .values(
            status=next_status,
            snapshot_json=next_snapshot,
            digest=digest_json(next_snapshot),
            lease_owner=None,
            lease_expires_at=None,
            claim_token=None,
        )
        .execution_options(synchronize_session=False)
    )
    if answered.rowcount != 1:
        await session.rollback()
        raise HTTPException(409, "TaskCheckpoint 回答已被并发消费")
    await append_agent_event(
        session,
        checkpoint.source_id,
        "agent.handoff.user_answered",
        {
            "taskCheckpointId": checkpoint.id,
            "continue": body.answer.get("continue") is True,
        },
    )
    await session.commit()
    return {"id": checkpoint.id, "status": next_status}


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
