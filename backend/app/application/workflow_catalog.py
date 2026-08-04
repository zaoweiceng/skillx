from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.domain.workflow import NodeType, WorkflowDefinition
from app.infrastructure.database import (
    Run,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowFragmentManifest,
    WorkflowVersion,
)

MAX_SEARCH_LIMIT = 8
MAX_SEARCH_BYTES = 24_000
_SECRET_KEYS = re.compile(r"secret|token|password|prompt|artifact", re.I)
_SESSION_LIMITS = {"search": 20, "describe": 40, "slice": 10, "plan": 20}
_SESSION_CALLS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
_SESSION_REFS: dict[str, set[str]] = defaultdict(set)


class CatalogError(ValueError):
    pass


def enforce_session_limit(session_id: str, operation: str) -> None:
    limit = _SESSION_LIMITS[operation]
    _SESSION_CALLS[session_id][operation] += 1
    if _SESSION_CALLS[session_id][operation] > limit:
        raise CatalogError(f"会话 {operation} 调用超过上限 {limit}")


def disclose_refs(session_id: str, refs: list[str]) -> None:
    _SESSION_REFS[session_id].update(refs)


def require_disclosed_ref(session_id: str, ref: str) -> None:
    if ref not in _SESSION_REFS[session_id]:
        raise CatalogError("ref 未由当前会话的 Registry 渐进披露")


def require_plan_refs_disclosed(session_id: str, raw_plan: dict[str, Any]) -> None:
    for step in raw_plan.get("steps", []):
        ref = step.get("ref") if isinstance(step, dict) else None
        if ref:
            require_disclosed_ref(session_id, str(ref))


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
    return {
        key: value
        for key, value in raw.items()
        if key in {"summary", "useWhen", "doNotUseWhen", "examples", "tags"}
        and not _SECRET_KEYS.search(key)
    }


async def build_manifest(
    session: AsyncSession,
    workflow: Workflow,
    version: WorkflowVersion,
) -> WorkflowCapabilityManifest:
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
    }
    search_text = " ".join(
        [
            workflow.name,
            str(manifest["summary"]),
            *map(str, manifest["tags"]),
            *map(str, manifest["useWhen"]),
            *map(str, manifest["doNotUseWhen"]),
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
    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in definition.spec.edges:
        incoming[edge.target].add(edge.source)
    results: list[WorkflowFragmentManifest] = []
    for raw in declarations:
        name = str(raw.get("name", ""))
        declared = set(raw.get("nodeIds", []))
        if not name or not declared or not declared <= nodes.keys():
            raise CatalogError("callable fragment 需要合法 name/nodeIds")
        for node_id in declared:
            hidden = {
                source
                for source in incoming[node_id]
                if source not in declared and nodes[source].type != NodeType.INPUT
            }
            if hidden:
                raise CatalogError(f"fragment {name} 存在隐含依赖: {sorted(hidden)}")
        side_effects = [nodes[node_id] for node_id in declared if nodes[node_id].side_effect]
        approvals = [
            nodes[node_id]
            for node_id in declared
            if nodes[node_id].type == NodeType.APPROVAL
        ]
        if side_effects and not approvals and not raw.get("allowSideEffects", False):
            raise CatalogError(f"fragment {name} 的副作用缺少显式允许或审批")
        closure = sorted(declared)
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
            "boundaryInputSchema": raw.get(
                "inputSchema", definition.spec.input_schema
            ),
            "boundaryOutputSchema": raw.get(
                "outputSchema", definition.spec.output_schema
            ),
            "inputMapping": raw.get("inputMapping", {}),
            "outputMapping": raw.get("outputMapping", {}),
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
    rows = (
        await session.scalars(
            select(WorkflowCapabilityManifest).where(
                WorkflowCapabilityManifest.status == "published"
            )
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
                "inputSynopsis",
                "outputSynopsis",
                "executionClass",
                "sideEffects",
                "approvalRequired",
                "estimatedCost",
            )
        }
        card["manifestDigest"] = row.digest
        card["matchReason"] = {"score": score, "query": query[:200]}
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
                WorkflowCapabilityManifest.status == "published"
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
            WorkflowFragmentManifest.status.in_(["published", "private"]),
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
        **manifest,
        "manifestDigest": item.digest,
        "inputSchema": spec["inputSchema"],
        "outputSchema": spec["outputSchema"],
        "limits": spec.get("policies", {}),
        "versionLock": {"versionId": version.id, "digest": version.digest},
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
    incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for edge in definition.spec.edges:
        incoming[edge.target].add(edge.source)
    closure: set[str] = set()

    def include_ancestors(node_id: str) -> None:
        for source in incoming[node_id]:
            if nodes[source].type == NodeType.INPUT or source in closure:
                continue
            closure.add(source)
            include_ancestors(source)

    closure.update(candidates)
    for node_id in list(candidates):
        include_ancestors(node_id)
        fallback = nodes[node_id].fallback
        if fallback is not None:
            closure.add(fallback.target)
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
        "boundaryInputSchema": definition.spec.input_schema,
        "boundaryOutputSchema": output_schema,
        "inputMapping": {},
        "outputMapping": output_mapping,
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
