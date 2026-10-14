"""Exact, run-local approval plans for statically resolvable SkillX operations."""

from sqlalchemy import select

from app.application.compiler import digest_json
from app.application.execution_sources import load_execution_source
from app.application.expressions import ExpressionError, resolve_mapping
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import CapabilityVersion, NodeRun, Run


async def approval_plan(session, run_id):
    run = await session.get(Run, run_id)
    if (
        not run
        or run.cancellation_requested_at
        or run.status in {"failed", "cancelled", "succeeded"}
    ):
        return None
    attempts = list(await session.scalars(select(NodeRun).where(NodeRun.run_id == run_id)))
    if any(n.attempt != 1 or n.status in {"failed", "cancelled"} for n in attempts):
        return None
    source = await load_execution_source(session, run)
    if not source:
        return None
    definition = WorkflowDefinition.model_validate(source.definition_json)
    static_values = static_passthrough_values(definition, run.input_json)
    operations = []
    for node in definition.spec.nodes:
        if node.type.value != "skillx":
            continue
        version = await session.get(CapabilityVersion, node.config.get("capabilityVersionId"))
        if not version or version.status != "published":
            continue
        name = node.config.get("interface")
        raw = (
            version.manifest_json.get("manifest", {})
            .get("spec", {})
            .get("interfaces", {})
            .get(name, {})
        )
        # Agent and nested calls may select effects or arguments at runtime. They
        # retain individual approvals, as do expressions depending on node output.
        if raw.get("kind") != "direct" or raw.get("capabilities"):
            continue
        try:
            inputs = resolve_mapping(node.input, run.input_json, static_values)
        except (ExpressionError, KeyError, ValueError):
            continue
        from app.application.capabilities import resolve_package_path
        from app.runtime.skillx import package_digest

        if package_digest(resolve_package_path(version.package_path)) != version.digest:
            continue
        operations.append(
            {
                "nodeId": node.id,
                "title": node.display_name,
                "operation": {"versionId": version.id, "interface": name, "input": inputs},
                "versionDigest": version.digest,
                "manifestDigest": digest_json(version.manifest_json),
                "effects": raw.get("effects", []),
                "dependencyLocks": version.manifest_json.get("dependencyLocks", {}),
            }
        )
    runtime = run.runtime_snapshot_json or {}
    return {
        "runId": run.id,
        "source": {
            "id": source.id,
            "digest": source.digest,
            "definition": source.definition_json,
            "validation": source.validation_json,
        },
        "input": run.input_json,
        "runtime": {k: v for k, v in runtime.items() if k not in {"pausedSeconds", "waitingSince"}},
        "operations": operations,
    }


def covers(plan, node_id, operation, version_digest):
    return bool(
        plan
        and any(
            item["nodeId"] == node_id
            and item["operation"] == operation
            and item["versionDigest"] == version_digest
            for item in plan["operations"]
        )
    )


def static_passthrough_values(definition, inputs):
    """Evaluate only data mappings, never a capability or model operation."""
    values = {}
    pending = [
        n
        for n in definition.spec.nodes
        if n.type.value in {"transform", "output"}
        and (n.type.value == "output" or n.config.get("operation") == "passthrough")
    ]
    while pending:
        progress = False
        for node in list(pending):
            try:
                values[node.id] = resolve_mapping(node.input, inputs, values)
            except (ExpressionError, KeyError, ValueError):
                continue
            pending.remove(node)
            progress = True
        if not progress:
            break
    return values


async def operation_context(session, run_id, node_run_id, operation):
    """Bind one runtime operation to its actual resolved input and successful ancestry."""
    run = await session.get(Run, run_id)
    current = await session.get(NodeRun, node_run_id)
    if (
        not run
        or run.status in {"failed", "cancelled", "succeeded"}
        or run.cancellation_requested_at
        or not current
        or current.run_id != run.id
        or current.status not in {"running", "ready", "waiting_user"}
    ):
        return None
    source = await load_execution_source(session, run)
    if not source:
        return None
    definition = WorkflowDefinition.model_validate(source.definition_json)
    node = next((n for n in definition.spec.nodes if n.id == current.node_id), None)
    if (
        not node
        or node.type.value != "skillx"
        or current.node_type != "skillx"
        or node.config.get("capabilityVersionId") != operation.get("versionId")
        or node.config.get("interface") != operation.get("interface")
    ):
        return None
    records = list(
        await session.scalars(
            select(NodeRun).where(NodeRun.run_id == run.id).order_by(NodeRun.attempt)
        )
    )
    latest = {record.node_id: record for record in records}
    if latest.get(node.id).id != current.id:
        return None
    values = {
        key: n.output_json for key, n in latest.items() if n.status == "succeeded" and n.finished_at
    }
    try:
        inputs = resolve_mapping(node.input, run.input_json, values)
    except (ExpressionError, KeyError, ValueError):
        return None
    if digest_json(inputs) != digest_json(operation.get("input")) or digest_json(
        current.input_json
    ) != digest_json(inputs):
        return None
    version = await session.get(CapabilityVersion, operation["versionId"])
    if not version or version.status != "published":
        return None
    lock = next(
        (
            lock
            for lock in source.validation_json.get("capabilityLocks", [])
            if lock.get("nodeId") == node.id
        ),
        None,
    )
    if (
        not lock
        or lock.get("capabilityVersionId", lock.get("versionId")) != version.id
        or lock.get("interface") != operation["interface"]
        or lock.get("digest") != version.digest
    ):
        return None
    from app.application.capabilities import resolve_package_path
    from app.runtime.skillx import package_digest

    if package_digest(resolve_package_path(version.package_path)) != version.digest:
        return None
    raw = (
        version.manifest_json.get("manifest", {})
        .get("spec", {})
        .get("interfaces", {})
        .get(operation["interface"], {})
    )
    # Walk graph ancestry, not only directly mentioned references: an upstream
    # passthrough may conceal the capability that produced this exact value.
    ancestors = set()
    frontier = [node.id]
    while frontier:
        target = frontier.pop()
        for edge in definition.spec.edges:
            if edge.target == target and edge.source not in ancestors:
                ancestors.add(edge.source)
                frontier.append(edge.source)
    upstream = []
    for key in sorted(ancestors):
        record = latest.get(key)
        if not record or record.status not in {"succeeded", "skipped"}:
            return None
        if record.status == "succeeded" and not record.finished_at:
            return None
        upstream.append(
            {
                "nodeId": key,
                "nodeRunId": record.id,
                "attempt": record.attempt,
                "status": record.status,
                "outputDigest": digest_json(record.output_json),
            }
        )
    runtime = run.runtime_snapshot_json or {}
    return {
        "runId": run.id,
        "nodeRunId": current.id,
        "nodeId": node.id,
        "attempt": current.attempt,
        "sourceId": source.id,
        "sourceDigest": source.digest,
        "definitionDigest": digest_json(source.definition_json),
        "validationDigest": digest_json(source.validation_json),
        "runInputDigest": digest_json(run.input_json),
        "inputDigest": digest_json(inputs),
        "operation": operation,
        "upstream": upstream,
        "versionDigest": version.digest,
        "manifestDigest": digest_json(version.manifest_json),
        "effects": raw.get("effects", []),
        "kind": raw.get("kind"),
        "capabilities": raw.get("capabilities", []),
        "dependencyLocks": version.manifest_json.get("dependencyLocks", {}),
        "runtimeDigest": digest_json(
            {k: v for k, v in runtime.items() if k not in {"pausedSeconds", "waitingSince"}}
        ),
    }
