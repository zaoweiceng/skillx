from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.capabilities import (
    CapabilityResolutionError,
    validate_workflow_capabilities,
)
from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.mcp import (
    MCPConfigurationError,
    validate_mcp_nodes,
)
from app.application.product_history import (
    normalize_presentation_policy,
    validate_presentation_decision,
    validate_presentation_template,
)
from app.application.runtime_profiles import workflow_profiles
from app.application.workflow_catalog import (
    CatalogError,
    build_manifest,
    capability_metadata_issues,
)
from app.infrastructure.database import (
    ReplayEvidence,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowVersion,
)


class PublicationError(Exception):
    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))

class PublishInput(Protocol):
    draft_revision: int
    draft_digest: str
    replay_evidence_id: str | None

compiler = WorkflowCompiler()

async def validate_workflow(
    workflow_id: str, session: AsyncSession
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise PublicationError(404, "工作流不存在")
    try:
        compiler.compile(item.draft_json)
        from app.application.workflow_nesting import expand_workflow
        compiled = compiler.compile(await expand_workflow(session, item.draft_json))
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


async def publish_workflow(
    workflow_id: str,
    body: PublishInput,
    session: AsyncSession,
    *, commit: bool = True,
) -> dict[str, Any]:
    item = await session.get(Workflow, workflow_id)
    if item is None:
        raise PublicationError(404, "工作流不存在")
    try:
        source_compiled = compiler.compile(item.draft_json)
        from app.application.workflow_nesting import expand_workflow
        compiled = compiler.compile(await expand_workflow(session, item.draft_json))
        metadata_issues = capability_metadata_issues(compiled.definition)
        if metadata_issues:
            raise PublicationError(
                422,
                detail={
                    "code": "CAPABILITY_METADATA_REQUIRED",
                    "message": "metadata.name 必须是非空字符串；目录元数据 summary 必须是非空字符串；useWhen/doNotUseWhen/examples/tags 必须是非空字符串数组，examples 不是 input/output 对象；可执行样例放在顶层 workflowExamples",
                    "fields": metadata_issues,
                },
            )
        execution_class, capability_locks = await validate_workflow_capabilities(
            session, compiled.definition
        )
        mcp_locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
        validate_presentation_decision(compiled.definition.metadata)
    except WorkflowValidationError as exc:
        raise PublicationError(
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
        raise PublicationError(
            422,
            detail={"code": "CAPABILITY_INCOMPATIBLE", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise PublicationError(
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
            raise PublicationError(
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
        or source_compiled.digest != body.draft_digest
    ):
        raise PublicationError(
            409,
            detail={
                "code": "DRAFT_SNAPSHOT_CONFLICT",
                "message": "草稿已变化，发布请求不是当前审核快照",
                "currentRevision": item.draft_revision,
                "currentDigest": source_compiled.digest,
            },
        )

    revision_metadata = dict(item.draft_metadata_json or {})
    revision_status = revision_metadata.get("status")
    if revision_status == "dirty_after_replay":
        raise PublicationError(
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
            raise PublicationError(
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
            raise PublicationError(
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
        raise PublicationError(
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
        raise PublicationError(
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
            "nodeProfiles": await workflow_profiles(session, compiled.definition),
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
        raise PublicationError(
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
        raise PublicationError(
            409,
            detail={
                "code": "PUBLISH_CONFLICT",
                "message": "发布领取已失效",
            },
        )
    if commit:
        await session.commit()
    else:
        await session.flush()
    await session.refresh(version)
    return await published_response(version)


