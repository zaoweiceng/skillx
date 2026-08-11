from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.domain.workflow import NodeType, WorkflowDefinition
from app.infrastructure.database import (
    Run,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowDisclosureCall,
    WorkflowDisclosureSession,
    WorkflowFragmentManifest,
    WorkflowVersion,
    utcnow,
)

MAX_SEARCH_LIMIT = 8
MAX_SEARCH_BYTES = 24_000
_SECRET_KEYS = re.compile(r"secret|token|password|prompt|artifact", re.I)
_SESSION_LIMITS = {"search": 20, "describe": 40, "slice": 10, "plan": 20}


class CatalogError(ValueError):
    pass


async def create_disclosure_session(
    session: AsyncSession,
    *,
    conversation_id: str | None = None,
    agent_run_id: str | None = None,
    ttl_minutes: int = 60,
) -> WorkflowDisclosureSession:
    item = WorkflowDisclosureSession(
        id=str(uuid4()),
        conversation_id=conversation_id,
        agent_run_id=agent_run_id,
        disclosed_json={},
        call_budget_json=dict(_SESSION_LIMITS),
        expires_at=utcnow() + timedelta(minutes=max(1, ttl_minutes)),
        status="active",
    )
    session.add(item)
    await session.flush()
    return item


async def _active_disclosure_session(
    session: AsyncSession, session_id: str
) -> WorkflowDisclosureSession:
    item = await session.get(WorkflowDisclosureSession, session_id)
    if item is None:
        raise CatalogError("disclosure session 不存在")
    expires_at = item.expires_at
    if expires_at.tzinfo is None:
        from datetime import UTC

        expires_at = expires_at.replace(tzinfo=UTC)
    if item.status != "active" or expires_at <= utcnow():
        if item.status == "active":
            item.status = "expired"
        raise CatalogError("disclosure session 已过期或关闭")
    return item


async def consume_session_call(
    session: AsyncSession,
    session_id: str,
    operation: str,
    request_summary: dict[str, Any],
) -> WorkflowDisclosureCall:
    await _active_disclosure_session(session, session_id)
    if operation not in _SESSION_LIMITS:
        raise CatalogError(f"未知 disclosure operation: {operation}")
    json_path = f"$.{operation}"
    consumed = await session.execute(
        update(WorkflowDisclosureSession)
        .where(
            WorkflowDisclosureSession.id == session_id,
            WorkflowDisclosureSession.status == "active",
            WorkflowDisclosureSession.expires_at > utcnow(),
            func.json_extract(
                WorkflowDisclosureSession.call_budget_json, json_path
            )
            > 0,
        )
        .values(
            call_budget_json=func.json_set(
                WorkflowDisclosureSession.call_budget_json,
                json_path,
                func.json_extract(
                    WorkflowDisclosureSession.call_budget_json, json_path
                )
                - 1,
            )
        )
    )
    if consumed.rowcount != 1:
        raise CatalogError(f"会话 {operation} 调用超过上限")
    item = await session.get(WorkflowDisclosureSession, session_id)
    assert item is not None
    await session.refresh(item)
    call = WorkflowDisclosureCall(
        id=str(uuid4()),
        session_id=item.id,
        operation=operation,
        request_summary_json=request_summary,
        response_digest="pending",
        disclosure_level=None,
        status="pending",
    )
    session.add(call)
    await session.flush()
    return call


async def record_disclosure_response(
    session: AsyncSession,
    call_id: str,
    response: Any,
    *,
    level: str | None,
) -> None:
    recorded = await session.execute(
        update(WorkflowDisclosureCall)
        .where(
            WorkflowDisclosureCall.id == call_id,
            WorkflowDisclosureCall.response_digest == "pending",
            WorkflowDisclosureCall.status == "pending",
        )
        .values(
            response_digest=digest_json(response),
            disclosure_level=level,
            status="succeeded",
        )
    )
    if recorded.rowcount != 1:
        raise CatalogError("disclosure call 不存在或已闭合")


async def disclose_refs(
    session: AsyncSession,
    session_id: str,
    refs: list[str],
    level: str,
) -> None:
    item = await _active_disclosure_session(session, session_id)
    disclosed = dict(item.disclosed_json or {})
    rank = {"L0": 0, "L1": 1, "L2": 2}
    for ref in refs:
        previous = disclosed.get(ref)
        if previous is None or rank[level] > rank.get(previous, -1):
            disclosed[ref] = level
    item.disclosed_json = disclosed
    await session.flush()


async def require_disclosed_ref(
    session: AsyncSession, session_id: str, ref: str
) -> str:
    item = await _active_disclosure_session(session, session_id)
    level = (item.disclosed_json or {}).get(ref)
    if level is None:
        raise CatalogError("ref 未由当前会话的 Registry 渐进披露")
    return level


async def require_plan_refs_disclosed(
    session: AsyncSession, session_id: str, raw_plan: dict[str, Any]
) -> None:
    for step in raw_plan.get("steps", []):
        ref = step.get("ref") if isinstance(step, dict) else None
        if ref:
            await require_disclosed_ref(session, session_id, str(ref))


def workflow_ref(version: WorkflowVersion) -> str:
    return f"workflow:{version.workflow_id}@{version.version}#{version.digest}"


def _synopsis(schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    return {
        "required": list(schema.get("required", [])),
        "properties": {
            key: value.get("type", "unknown")
            for key, value in properties.items()
            if isinstance(value, dict)
        },
    }


def _semantic_metadata(definition: WorkflowDefinition) -> dict[str, Any]:
    raw = definition.metadata.get("capability", {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in {"summary", "useWhen", "doNotUseWhen", "examples", "tags"}:
            continue
        encoded = json.dumps(value, ensure_ascii=False)
        if (
            _SECRET_KEYS.search(key)
            or len(encoded.encode()) > 4096
            or re.search(r"(?:api[_-]?key|token|password)\s*[:=]|https?://[^/\s]+@", encoded, re.I)
        ):
            raise CatalogError(f"capability metadata {key} 含敏感值或超过长度限制")
        result[key] = value
    return result


def capability_metadata_issues(definition: WorkflowDefinition) -> list[str]:
    raw = definition.metadata.get("capability")
    if not isinstance(raw, dict):
        return ["metadata.capability"]
    issues: list[str] = []
    for field in ("summary", "useWhen", "doNotUseWhen", "examples", "tags"):
        value = raw.get(field)
        valid = (
            isinstance(value, str)
            and bool(value.strip())
            if field == "summary"
            else isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value)
        )
        if not valid:
            issues.append(f"metadata.capability.{field}")
    return issues


_INPUT_REF = re.compile(r"\$\{input\.([^}.]+)(?:\.[^}]*)?}")
_NODE_REF = re.compile(r"\$\{([a-zA-Z][\w-]*)\.(?:output\.)?[^}]+}")


def _refs(value: Any, pattern: re.Pattern[str]) -> set[str]:
    encoded = json.dumps(value, ensure_ascii=False)
    return set(pattern.findall(encoded))


def _fragment_closure(
    definition: WorkflowDefinition, declared: set[str]
) -> set[str]:
    nodes = {node.id: node for node in definition.spec.nodes}
    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in definition.spec.edges:
        incoming[edge.target].add(edge.source)
    closure = set(declared)
    changed = True
    while changed:
        changed = False
        for node_id in list(closure):
            node = nodes[node_id]
            dependencies = {
                source
                for source in incoming[node_id]
                if nodes[source].type != NodeType.INPUT
            }
            dependencies.update(
                ref
                for ref in _refs(node.input, _NODE_REF)
                if ref in nodes and nodes[ref].type not in {NodeType.INPUT, NodeType.OUTPUT}
            )
            if node.fallback is not None:
                dependencies.add(node.fallback.target)
            for dependency in dependencies:
                if dependency not in closure:
                    closure.add(dependency)
                    changed = True
    return closure


def _fragment_ports(
    definition: WorkflowDefinition,
    node_ids: set[str],
    raw: dict[str, Any],
) -> dict[str, Any]:
    nodes = {node.id: node for node in definition.spec.nodes}
    boundary_input = raw.get("inputSchema", definition.spec.input_schema)
    boundary_output = raw.get("outputSchema", definition.spec.output_schema)
    input_mapping = dict(raw.get("inputMapping") or {})
    required_source_inputs = {
        ref
        for node_id in node_ids
        for ref in _refs(nodes[node_id].input, _INPUT_REF)
    }
    boundary_fields = set(boundary_input.get("properties", {}))
    for source_field in required_source_inputs:
        if source_field not in input_mapping and source_field in boundary_fields:
            input_mapping[source_field] = f"${{input.{source_field}}}"
    missing_sources = required_source_inputs - set(input_mapping)
    if missing_sources:
        raise CatalogError(
            f"fragment 输入端口未映射源 Workflow 字段: {sorted(missing_sources)}"
        )
    mapped_boundary = {
        ref for value in input_mapping.values() for ref in _refs(value, _INPUT_REF)
    }
    if not set(boundary_input.get("required", [])) <= mapped_boundary:
        raise CatalogError("fragment required boundary input 未映射到闭包节点输入")

    output_mapping = dict(raw.get("outputMapping") or {})
    if not output_mapping:
        output_node = next(
            node for node in definition.spec.nodes if node.type == NodeType.OUTPUT
        )
        output_mapping = {
            key: value
            for key, value in output_node.input.items()
            if _refs(value, _NODE_REF) <= node_ids
        }
    missing_outputs = set(boundary_output.get("required", [])) - set(output_mapping)
    if missing_outputs:
        raise CatalogError(
            f"fragment 缺少 required output mapping: {sorted(missing_outputs)}"
        )
    output_sources = {
        ref for value in output_mapping.values() for ref in _refs(value, _NODE_REF)
    }
    if not output_sources <= node_ids:
        raise CatalogError("fragment output mapping 引用了闭包外节点")
    entry_nodes = sorted(
        node_id
        for node_id in node_ids
        if any(
            edge.target == node_id and edge.source not in node_ids
            for edge in definition.spec.edges
        )
        or _refs(nodes[node_id].input, _INPUT_REF)
    )
    exit_nodes = sorted(output_sources)
    return {
        "entryPorts": entry_nodes,
        "exitPorts": exit_nodes,
        "boundaryInputSchema": boundary_input,
        "boundaryOutputSchema": boundary_output,
        "inputMapping": input_mapping,
        "outputMapping": output_mapping,
    }


async def build_manifest(
    session: AsyncSession,
    workflow: Workflow,
    version: WorkflowVersion,
) -> WorkflowCapabilityManifest:
    previous = (
        await session.scalars(
            select(WorkflowCapabilityManifest)
            .join(
                WorkflowVersion,
                WorkflowVersion.id
                == WorkflowCapabilityManifest.workflow_version_id,
            )
            .where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowCapabilityManifest.status == "published",
                WorkflowCapabilityManifest.workflow_version_id != version.id,
            )
        )
    ).all()
    for item in previous:
        item.status = "resolvable"
    previous_fragments = (
        await session.scalars(
            select(WorkflowFragmentManifest)
            .join(
                WorkflowVersion,
                WorkflowVersion.id
                == WorkflowFragmentManifest.workflow_version_id,
            )
            .where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowFragmentManifest.status == "published",
                WorkflowFragmentManifest.workflow_version_id != version.id,
            )
        )
    ).all()
    for item in previous_fragments:
        item.status = "resolvable"
    definition = WorkflowDefinition.model_validate(version.definition_json)
    semantic = _semantic_metadata(definition)
    nodes = [
        node
        for node in definition.spec.nodes
        if node.type not in {NodeType.INPUT, NodeType.OUTPUT}
    ]
    side_effects = [node for node in nodes if node.side_effect]
    approvals = [
        node
        for node in nodes
        if node.type == NodeType.APPROVAL
        or (node.fallback is not None and node.fallback.require_user_approval)
    ]
    execution_class = version.validation_json.get("executionClass", "zero_llm")
    ref = workflow_ref(version)
    lineage = definition.metadata.get("lineage", {})
    manifest = {
        "workflowRef": ref,
        "name": workflow.name,
        "summary": semantic.get("summary", workflow.name),
        "useWhen": semantic.get("useWhen", []),
        "doNotUseWhen": semantic.get("doNotUseWhen", []),
        "examples": semantic.get("examples", []),
        "tags": semantic.get("tags", []),
        "inputSynopsis": _synopsis(definition.spec.input_schema),
        "outputSynopsis": _synopsis(definition.spec.output_schema),
        "executionClass": execution_class,
        "sideEffects": "none" if not side_effects else "declared",
        "approvalRequired": bool(approvals),
        "estimatedCost": {
            "llmCalls": sum(node.type in {NodeType.LLM, NodeType.AGENT} for node in nodes)
        },
        "entrypoints": ["full"],
        "stepSummary": [
            {"name": node.display_name, "type": node.type.value} for node in nodes[:12]
        ],
        "capabilityLocks": version.validation_json.get("capabilityLocks", []),
        "mcpLocks": version.validation_json.get("mcpLocks", []),
        "workflowVersionId": version.id,
        "workflowDigest": version.digest,
        "lineage": {
            key: lineage[key]
            for key in (
                "sourceConversationRunId",
                "sourceTrajectoryDigest",
                "distillationId",
            )
            if key in lineage
        },
        "revisionLineage": dict(version.revision_lineage_json or {}),
    }
    search_text = " ".join(
        [
            workflow.name,
            str(manifest["summary"]),
            *map(str, manifest["tags"]),
            *map(str, manifest["useWhen"]),
            *map(str, manifest["examples"]),
        ]
    ).lower()
    item = WorkflowCapabilityManifest(
        id=str(uuid4()),
        workflow_version_id=version.id,
        workflow_digest=version.digest,
        manifest_json=manifest,
        digest=digest_json(manifest),
        status="published",
        search_text=search_text,
    )
    session.add(item)
    await session.flush()
    fragments = await _build_fragments(session, version, definition)
    if fragments:
        manifest = {
            **manifest,
            "entrypoints": [
                *manifest["entrypoints"],
                *(fragment.manifest_json["name"] for fragment in fragments),
            ],
            "fragments": [
                {
                    "fragmentRef": fragment.fragment_ref,
                    "name": fragment.manifest_json["name"],
                    "summary": fragment.manifest_json.get("summary", ""),
                }
                for fragment in fragments
            ],
        }
        item.manifest_json = manifest
        item.digest = digest_json(manifest)
    return item


async def _build_fragments(
    session: AsyncSession,
    version: WorkflowVersion,
    definition: WorkflowDefinition,
) -> list[WorkflowFragmentManifest]:
    declarations = definition.metadata.get("callableFragments", [])
    if not isinstance(declarations, list):
        raise CatalogError("metadata.callableFragments 必须是数组")
    nodes = {node.id: node for node in definition.spec.nodes}
    results: list[WorkflowFragmentManifest] = []
    for raw in declarations:
        name = str(raw.get("name", ""))
        declared = set(raw.get("nodeIds", []))
        if not name or not declared or not declared <= nodes.keys():
            raise CatalogError("callable fragment 需要合法 name/nodeIds")
        closure_set = _fragment_closure(definition, declared)
        if closure_set != declared:
            raise CatalogError(
                f"fragment {name} 存在隐藏依赖，必须显式包含: {sorted(closure_set - declared)}"
            )
        side_effects = [nodes[node_id] for node_id in closure_set if nodes[node_id].side_effect]
        approvals = [
            nodes[node_id]
            for node_id in closure_set
            if nodes[node_id].type == NodeType.APPROVAL
        ]
        if side_effects and not approvals:
            raise CatalogError(f"fragment {name} 的副作用必须保留源审批边界")
        closure = sorted(closure_set)
        ports = _fragment_ports(definition, closure_set, raw)
        closure_digest = digest_json(
            {
                "workflowDigest": version.digest,
                "nodes": [
                    nodes[node_id].model_dump(by_alias=True, mode="json")
                    for node_id in closure
                ],
            }
        )
        fragment_ref = (
            f"fragment:{version.workflow_id}@{version.version}/{name}#{closure_digest}"
        )
        manifest = {
            "fragmentRef": fragment_ref,
            "name": name,
            "summary": raw.get("summary", name),
            "useWhen": raw.get("useWhen", []),
            "doNotUseWhen": raw.get("doNotUseWhen", []),
            "sourceWorkflowRef": workflow_ref(version),
            "sourceWorkflowVersionId": version.id,
            "sourceWorkflowDigest": version.digest,
            "nodeIds": closure,
            **ports,
            "portDigest": digest_json(ports),
            "sideEffects": "none" if not side_effects else "declared",
            "approvalRequired": bool(approvals),
            "capabilityLocks": [
                lock
                for lock in version.validation_json.get("capabilityLocks", [])
                if lock.get("nodeId") in declared
            ],
            "mcpLocks": [
                lock
                for lock in version.validation_json.get("mcpLocks", [])
                if lock.get("nodeId") in declared
            ],
            "closureDigest": closure_digest,
        }
        item = WorkflowFragmentManifest(
            id=str(uuid4()),
            workflow_version_id=version.id,
            fragment_ref=fragment_ref,
            source_workflow_digest=version.digest,
            closure_digest=closure_digest,
            manifest_json=manifest,
            digest=digest_json(manifest),
            status="published",
        )
        session.add(item)
        results.append(item)
    await session.flush()
    return results


def _score(query: str, search_text: str) -> float:
    query = query.strip().lower()
    if not query:
        return 0.0
    terms = [term for term in re.split(r"\s+", query) if term]
    term_score = sum(term in search_text for term in terms) / max(len(terms), 1)
    substring = 1.0 if query in search_text else 0.0
    # 中文无空格查询仍可通过二元字符覆盖率获得稳定排序。
    grams = {query[index : index + 2] for index in range(max(1, len(query) - 1))}
    gram_score = sum(gram in search_text for gram in grams) / max(len(grams), 1)
    return round(0.5 * term_score + 0.3 * substring + 0.2 * gram_score, 4)


def _matches_constraints(manifest: dict[str, Any], constraints: dict[str, Any]) -> bool:
    required_inputs = set(constraints.get("requiredInputs", []))
    available_inputs = set(manifest["inputSynopsis"].get("required", []))
    if required_inputs and not required_inputs <= available_inputs:
        return False
    if constraints.get("zeroLlm") and manifest["estimatedCost"].get("llmCalls", 0):
        return False
    if constraints.get("sideEffects") == "none" and manifest["sideEffects"] != "none":
        return False
    output_type = constraints.get("outputType")
    if output_type and output_type not in manifest["outputSynopsis"].get("properties", {}).values():
        return False
    return True


async def search_manifests(
    session: AsyncSession,
    query: str,
    constraints: dict[str, Any] | None = None,
    limit: int = MAX_SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    constraints = constraints or {}
    normalized_query = query.strip().lower()[:200]
    prefilter = select(WorkflowCapabilityManifest).where(
        WorkflowCapabilityManifest.status == "published"
    )
    terms = [term for term in re.split(r"\s+", normalized_query) if term]
    if terms:
        prefilter = prefilter.where(
            WorkflowCapabilityManifest.search_text.contains(terms[0])
        )
    rows = (
        await session.scalars(
            prefilter.order_by(WorkflowCapabilityManifest.updated_at.desc()).limit(256)
        )
    ).all()
    if not rows and terms:
        rows = (
            await session.scalars(
                select(WorkflowCapabilityManifest)
                .where(WorkflowCapabilityManifest.status == "published")
                .order_by(WorkflowCapabilityManifest.updated_at.desc())
                .limit(256)
            )
        ).all()
    candidates: list[tuple[float, WorkflowCapabilityManifest]] = []
    for row in rows:
        if not _matches_constraints(row.manifest_json, constraints):
            continue
        score = _score(query, row.search_text)
        if score > 0:
            candidates.append((score, row))
    candidates.sort(key=lambda item: (-item[0], item[1].manifest_json["name"]))
    result: list[dict[str, Any]] = []
    total = 2
    for score, row in candidates[:limit]:
        manifest = row.manifest_json
        card = {
            key: manifest[key]
            for key in (
                "workflowRef",
                "name",
                "summary",
                "tags",
            )
        }
        card["manifestDigest"] = row.digest
        normalized = query.strip().lower()
        matched = [
            value
            for value in [
                manifest["name"],
                manifest["summary"],
                *manifest.get("tags", []),
                *manifest.get("useWhen", []),
                *manifest.get("examples", []),
            ]
            if normalized and _score(normalized, str(value).lower()) > 0
        ][:3]
        card["matchReason"] = {
            "score": score,
            "query": query[:200],
            "summary": (
                f"命中：{'；'.join(map(str, matched))}"
                if matched
                else "名称、标签或适用场景存在语义召回"
            ),
        }
        encoded = len(json.dumps(card, ensure_ascii=False).encode())
        if total + encoded > MAX_SEARCH_BYTES:
            break
        result.append(card)
        total += encoded
    return result


async def resolve_workflow_manifest(
    session: AsyncSession, ref: str
) -> WorkflowCapabilityManifest:
    rows = (
        await session.scalars(
            select(WorkflowCapabilityManifest).where(
                WorkflowCapabilityManifest.status.in_(["published", "resolvable"])
            )
        )
    ).all()
    item = next((row for row in rows if row.manifest_json.get("workflowRef") == ref), None)
    if item is None:
        raise CatalogError("workflowRef 不存在或未发布")
    return item


async def resolve_fragment_manifest(
    session: AsyncSession, ref: str
) -> WorkflowFragmentManifest:
    item = await session.scalar(
        select(WorkflowFragmentManifest).where(
            WorkflowFragmentManifest.fragment_ref == ref,
            WorkflowFragmentManifest.status.in_(
                ["published", "resolvable", "private"]
            ),
        )
    )
    if item is None:
        raise CatalogError("fragmentRef 不存在或未发布")
    return item


async def describe_workflow(session: AsyncSession, ref: str) -> dict[str, Any]:
    item = await resolve_workflow_manifest(session, ref)
    version = await session.get(WorkflowVersion, item.workflow_version_id)
    if version is None:
        raise CatalogError("workflow version 不存在")
    spec = version.definition_json["spec"]
    manifest = item.manifest_json
    recent = (
        await session.scalars(
            select(Run)
            .where(Run.workflow_version_id == version.id)
            .order_by(Run.created_at.desc())
            .limit(20)
        )
    ).all()
    terminal = [run for run in recent if run.status in {"succeeded", "failed", "cancelled"}]
    return {
        **{
            key: manifest[key]
            for key in (
                "workflowRef",
                "name",
                "summary",
                "useWhen",
                "doNotUseWhen",
                "examples",
                "tags",
                "inputSynopsis",
                "outputSynopsis",
                "executionClass",
                "sideEffects",
                "approvalRequired",
                "estimatedCost",
                "entrypoints",
            )
        },
        "fragments": manifest.get("fragments", []),
        "manifestDigest": item.digest,
        "limits": spec.get("policies", {}),
        "reliability": {
            "sampleSize": len(terminal),
            "successRate": (
                round(sum(run.status == "succeeded" for run in terminal) / len(terminal), 3)
                if terminal
                else None
            ),
        },
    }


async def describe_fragment(session: AsyncSession, ref: str) -> dict[str, Any]:
    item = await resolve_fragment_manifest(session, ref)
    return {**item.manifest_json, "manifestDigest": item.digest}


async def create_private_fragment_snapshot(
    session: AsyncSession,
    workflow_ref_value: str,
    candidate_node_ids: list[str],
) -> WorkflowFragmentManifest:
    source_manifest = await resolve_workflow_manifest(session, workflow_ref_value)
    version = await session.get(WorkflowVersion, source_manifest.workflow_version_id)
    if version is None:
        raise CatalogError("source workflow version 不存在")
    definition = WorkflowDefinition.model_validate(version.definition_json)
    nodes = {node.id: node for node in definition.spec.nodes}
    candidates = set(candidate_node_ids)
    if not candidates or not candidates <= nodes.keys():
        raise CatalogError("裁剪目标节点不存在")
    if any(nodes[node_id].type in {NodeType.INPUT, NodeType.OUTPUT} for node_id in candidates):
        raise CatalogError("Input/Output 不是可裁剪业务节点")
    closure = _fragment_closure(definition, candidates)
    business_nodes = [nodes[node_id] for node_id in closure]
    if any(node.side_effect for node in business_nodes) and not any(
        node.type == NodeType.APPROVAL for node in business_nodes
    ):
        raise CatalogError("裁剪跨越副作用但未包含审批，已拒绝")
    serialized = json.dumps(
        [node.model_dump(by_alias=True, mode="json") for node in business_nodes]
    )
    if re.search(r"secretRef|password|apiKey|token", serialized, re.I):
        raise CatalogError("裁剪需要隐藏 Secret，无法生成独立边界")
    output_node = next(
        node for node in definition.spec.nodes if node.type == NodeType.OUTPUT
    )
    output_mapping = {
        key: value
        for key, value in output_node.input.items()
        if isinstance(value, str)
        and any(f"${{{node_id}." in value for node_id in closure)
    }
    if not output_mapping:
        if len(candidates) != 1:
            raise CatalogError("裁剪缺少确定输出 mapping")
        selected = next(iter(candidates))
        output_mapping = {"result": f"${{{selected}.output}}"}
        output_schema = {
            "type": "object",
            "properties": {"result": {}},
            "required": ["result"],
        }
    else:
        output_schema = {
            **definition.spec.output_schema,
            "properties": {
                key: definition.spec.output_schema.get("properties", {}).get(key, {})
                for key in output_mapping
            },
            "required": [
                key
                for key in definition.spec.output_schema.get("required", [])
                if key in output_mapping
            ],
        }
    source_input_fields = {
        ref
        for node_id in closure
        for ref in _refs(nodes[node_id].input, _INPUT_REF)
    }
    source_properties = definition.spec.input_schema.get("properties", {})
    input_schema = {
        **definition.spec.input_schema,
        "properties": {
            key: source_properties.get(key, {}) for key in sorted(source_input_fields)
        },
        "required": [
            key
            for key in definition.spec.input_schema.get("required", [])
            if key in source_input_fields
        ],
    }
    ports = _fragment_ports(
        definition,
        closure,
        {
            "inputSchema": input_schema,
            "outputSchema": output_schema,
            "inputMapping": {
                key: f"${{input.{key}}}" for key in sorted(source_input_fields)
            },
            "outputMapping": output_mapping,
        },
    )
    closure_digest = digest_json(
        {"workflowDigest": version.digest, "nodeIds": sorted(closure)}
    )
    fragment_ref = (
        f"fragment-snapshot:{version.workflow_id}@{version.version}/"
        f"{uuid4()}#{closure_digest}"
    )
    data = {
        "fragmentRef": fragment_ref,
        "name": "temporary-slice",
        "summary": "由确定性 Slicer 生成的私有临时片段",
        "sourceWorkflowRef": workflow_ref_value,
        "sourceWorkflowVersionId": version.id,
        "sourceWorkflowDigest": version.digest,
        "nodeIds": sorted(closure),
        **ports,
        "portDigest": digest_json(ports),
        "sideEffects": (
            "declared" if any(node.side_effect for node in business_nodes) else "none"
        ),
        "approvalRequired": any(
            node.type == NodeType.APPROVAL for node in business_nodes
        ),
        "capabilityLocks": [
            lock
            for lock in version.validation_json.get("capabilityLocks", [])
            if lock.get("nodeId") in closure
        ],
        "mcpLocks": [
            lock
            for lock in version.validation_json.get("mcpLocks", [])
            if lock.get("nodeId") in closure
        ],
        "closureDigest": closure_digest,
        "private": True,
    }
    item = WorkflowFragmentManifest(
        id=str(uuid4()),
        workflow_version_id=version.id,
        fragment_ref=fragment_ref,
        source_workflow_digest=version.digest,
        closure_digest=closure_digest,
        manifest_json=data,
        digest=digest_json(data),
        status="private",
    )
    session.add(item)
    await session.flush()
    return item
