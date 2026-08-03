from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import WorkflowCompiler, WorkflowValidationError, digest_json
from app.application.orchestrator import Orchestrator, append_event
from app.config import get_settings
from app.infrastructure.database import (
    Approval,
    Artifact,
    Capability,
    CapabilityVersion,
    Component,
    Conversation,
    Message,
    NodeRun,
    Run,
    RunEvent,
    Workflow,
    WorkflowVersion,
    get_session,
    utcnow,
)
from app.runtime.skillx import load_interface

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


class ConversationCreate(BaseModel):
    title: str = "新对话"
    bound_component_id: str | None = Field(default=None, alias="boundComponentId")


class MessageCreate(BaseModel):
    content: str


def workflow_view(item: Workflow) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "draftRevision": item.draft_revision,
        "definition": item.draft_json,
        "createdAt": item.created_at,
        "updatedAt": item.updated_at,
    }


def run_view(run: Run, node_runs: list[NodeRun] | None = None) -> dict[str, Any]:
    payload = {
        "id": run.id,
        "workflowVersionId": run.workflow_version_id,
        "componentId": run.component_id,
        "status": run.status,
        "input": run.input_json,
        "output": run.output_json,
        "error": run.error_json,
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
                "input": item.input_json,
                "output": item.output_json,
                "error": item.error_json,
            }
            for item in node_runs
        ]
    return payload


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.scalar(select(func.count()).select_from(Workflow))
    return {"status": "ready"}


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
        return {
            "valid": True,
            "digest": compiled.digest,
            "topologicalOrder": compiled.topological_order,
            "diagnostics": compiled.diagnostics,
        }
    except WorkflowValidationError as exc:
        return {"valid": False, "diagnostics": exc.diagnostics}


@router.post("/workflows/{workflow_id}/publish", status_code=201)
async def publish_workflow(
    workflow_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise HTTPException(404, "工作流不存在")
    try:
        compiled = compiler.compile(item.draft_json)
    except WorkflowValidationError as exc:
        raise HTTPException(
            422,
            detail={"code": "WORKFLOW_VALIDATION_FAILED", "diagnostics": exc.diagnostics},
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
    item = Component(
        id=str(uuid4()),
        name=body.name,
        description=body.description,
        workflow_id=version.workflow_id,
        default_version_id=version.id,
        visibility=body.visibility,
    )
    session.add(item)
    await session.commit()
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "workflowId": item.workflow_id,
        "defaultVersionId": item.default_version_id,
    }


@router.get("/components/{component_id}")
async def get_component(
    component_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    item = await session.get(Component, component_id)
    if item is None:
        raise HTTPException(404, "组件不存在")
    version = await session.get(WorkflowVersion, item.default_version_id)
    return {
        "id": item.id,
        "name": item.name,
        "description": item.description,
        "workflowId": item.workflow_id,
        "defaultVersionId": item.default_version_id,
        "visibility": item.visibility,
        "definition": version.definition_json if version else None,
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
    result = run_view(run, list(nodes))
    result["approvals"] = [
        {
            "id": item.id,
            "nodeId": item.node_id,
            "status": item.status,
            "snapshotDigest": item.snapshot_digest,
            "snapshot": item.snapshot_json,
        }
        for item in approvals
    ]
    return result


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "运行不存在")
    if run.status not in {"succeeded", "failed", "cancelled"}:
        run.cancellation_requested_at = utcnow()
        await orchestrator.advance_run(session, run.id)
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
    node_run = await session.get(NodeRun, approval.node_run_id)
    if node_run is None or node_run.status != "waiting_user":
        raise HTTPException(409, "关联节点不在等待状态")
    approval.status = "approved"
    approval.decided_by = "local-user"
    approval.decided_at = utcnow()
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
    path = (root / artifact.run_id / artifact.name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(410, "Artifact 文件不可用")
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


@router.post("/capabilities", status_code=201)
async def register_capability(
    body: CapabilityCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    version_value = "0.0.0"
    if body.type == "skillx":
        if not body.package_path or not body.interface:
            raise HTTPException(422, "SkillX 注册需要 packagePath 和 interface")
        try:
            loaded = load_interface(Path(body.package_path), body.interface)
        except Exception as exc:
            raise HTTPException(
                422, detail={"code": "INVALID_SKILLX", "message": str(exc)}
            ) from exc
        manifest = loaded.manifest
        version_value = loaded.version
    capability = Capability(id=str(uuid4()), type=body.type, name=body.name, visibility="private")
    version = CapabilityVersion(
        id=str(uuid4()),
        capability_id=capability.id,
        version=version_value,
        manifest_json=manifest,
        digest=digest_json(manifest),
        status="published",
        package_path=body.package_path,
    )
    capability.current_version_id = version.id
    session.add_all([capability, version])
    await session.commit()
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
    assistant_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        role="assistant",
        content_json={
            "type": "error",
            "code": "MODEL_NOT_CONFIGURED",
            "text": "通用对话需要配置 LLM/Agent Runtime；系统未进行静态意图路由。",
        },
    )
    session.add_all([user_message, assistant_message])
    await session.commit()
    return {
        "userMessageId": user_message.id,
        "assistant": assistant_message.content_json,
    }
