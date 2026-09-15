"""Resolve immutable workflow, plan and recovery execution sources."""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import (
    ExecutionCheckpoint,
    PlanSnapshot,
    RecoveryAttempt,
    Run,
    WorkflowVersion,
)


@dataclass(frozen=True)
class ExecutionSource:
    id: str
    definition_json: dict[str, Any]
    validation_json: dict[str, Any]
    digest: str


async def load_execution_source(session: AsyncSession, run: Run) -> ExecutionSource | None:
    source: ExecutionSource | None = None
    if run.workflow_version_id:
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:
            return None
        source = ExecutionSource(
            version.id,
            version.definition_json,
            version.validation_json or {},
            version.digest,
        )
    elif run.plan_snapshot_id:
        from app.application.plans import verify_plan_snapshot

        snapshot = await session.get(PlanSnapshot, run.plan_snapshot_id)
        if snapshot is None:
            return None
        verify_plan_snapshot(snapshot, for_new_run=False)
        source = ExecutionSource(
            snapshot.id,
            snapshot.definition_json,
            {"capabilityLocks": snapshot.locks_json, "mcpLocks": snapshot.locks_json},
            snapshot.digest,
        )
    if source is None:
        return None
    recovery_attempt = await session.scalar(
        select(RecoveryAttempt)
        .join(
            ExecutionCheckpoint,
            ExecutionCheckpoint.id == RecoveryAttempt.checkpoint_id,
        )
        .where(
            ExecutionCheckpoint.run_id == run.id,
            RecoveryAttempt.status == "active",
        )
        .order_by(RecoveryAttempt.created_at.desc())
        .limit(1)
    )
    if recovery_attempt is None or not recovery_attempt.plan_patch_json:
        return source
    frozen_definition = recovery_attempt.plan_patch_json.get("compiledDefinition")
    if isinstance(frozen_definition, dict):
        return ExecutionSource(
            recovery_attempt.id,
            frozen_definition,
            {
                "capabilityLocks": recovery_attempt.plan_patch_json.get("generatedLocks", []),
                "mcpLocks": recovery_attempt.plan_patch_json.get("generatedLocks", []),
            },
            recovery_attempt.plan_patch_json.get("afterDigest", recovery_attempt.digest),
        )
    patched_definition = _merge_recovery_plan(
        source.definition_json, recovery_attempt.plan_patch_json
    )
    from app.application.compiler import WorkflowCompiler

    compiled = WorkflowCompiler().compile(patched_definition)
    return ExecutionSource(
        recovery_attempt.id,
        compiled.definition.model_dump(by_alias=True, mode="json"),
        source.validation_json,
        recovery_attempt.digest,
    )


def _merge_recovery_plan(base_definition: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把已校验 RecoveryPlanPatch 合并到冻结定义，不修改原版本。"""

    definition = {
        **base_definition,
        "spec": {
            **base_definition["spec"],
            "nodes": [dict(node) for node in base_definition["spec"]["nodes"]],
            "edges": [dict(edge) for edge in base_definition["spec"]["edges"]],
        },
    }
    replacements = {}
    for item in patch.get("nodePatches", []):
        node_id = item.get("nodeId")
        source = next(
            (node for node in definition["spec"]["nodes"] if node["id"] == node_id),
            None,
        )
        if source is not None:
            replacement = {**source, **item.get("changes", {})}
            if isinstance(item.get("changes", {}).get("config"), dict):
                replacement["config"] = {
                    **source.get("config", {}),
                    **item["changes"]["config"],
                }
            replacements[node_id] = replacement
    input_mappings = patch.get("inputMappings", {})
    nodes = []
    for node in definition["spec"]["nodes"]:
        replacement = replacements.get(node["id"], node)
        if node["id"] in input_mappings:
            replacement = {**replacement, "input": input_mappings[node["id"]]}
        nodes.append(replacement)
    definition["spec"]["nodes"] = nodes
    edges = definition["spec"]["edges"]
    for edge_patch in patch.get("edgePatches", []):
        candidate = {
            "source": edge_patch["source"],
            "target": edge_patch["target"],
            **({"when": edge_patch["when"]} if edge_patch.get("when") is not None else {}),
        }
        if edge_patch.get("action") == "remove":
            edges[:] = [
                edge
                for edge in edges
                if not (
                    edge["source"] == candidate["source"]
                    and edge["target"] == candidate["target"]
                    and edge.get("when") == candidate.get("when")
                )
            ]
        elif candidate not in edges:
            edges.append(candidate)
    return definition
