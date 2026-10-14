"""Ground final output coverage in immutable execution records, not node labels.

Coverage means this exact value came from a successful node output. It does not
assert business invariants, command stdout, or current external resource state.
"""

import re

from sqlalchemy import select

from app.application.compiler import digest_json
from app.application.execution_sources import load_execution_source
from app.application.expressions import EXACT_REFERENCE, ExpressionError, resolve_value
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import CapabilityVersion, NodeRun, Run
from app.infrastructure.models.base import rfc3339


def output_coverage(mapping, output, nodes, definitions=None):
    """Only exact references with present, equal values qualify; no interpolation."""
    coverage = []
    values = {key: node.output_json for key, node in nodes.items() if node.status == "succeeded"}

    definitions = definitions or {}

    def render(parts):
        return "".join(
            part if part.startswith("[") else ("." if index else "") + part
            for index, part in enumerate(parts)
        )

    def trace(node_id, output_path, actual, seen):
        endpoint = {"nodeId": node_id, "nodeOutputPath": output_path}
        if (node_id, output_path) in seen:
            return endpoint
        seen = seen | {(node_id, output_path)}
        definition = definitions.get(node_id)
        if not definition or not (
            definition.type.value == "output"
            or definition.type.value == "transform"
            and definition.config.get("operation") == "passthrough"
        ):
            return endpoint
        parts = re.findall(r"[a-zA-Z0-9_-]+|\[\d+\]", output_path)
        mapped = definition.input
        for index in range(len(parts) + 1):
            match = EXACT_REFERENCE.fullmatch(mapped) if isinstance(mapped, str) else None
            if match and match["root"] != "input" and match["path"].startswith("output."):
                suffix = render(parts[index:])
                target_path = match["path"].removeprefix("output.")
                if suffix:
                    target_path += ("" if suffix.startswith("[") else ".") + suffix
                record = nodes.get(match["root"])
                if not record or record.status != "succeeded" or not record.finished_at:
                    return endpoint
                try:
                    resolved = resolve_value(
                        "${" + match["root"] + ".output." + target_path + "}", {}, values
                    )
                except (ExpressionError, KeyError, ValueError):
                    return endpoint
                if digest_json(resolved) != digest_json(actual):
                    return endpoint
                return trace(match["root"], target_path, actual, seen)
            if index == len(parts):
                break
            part = parts[index]
            if part.startswith("[") and isinstance(mapped, list):
                offset = int(part[1:-1])
                if offset >= len(mapped):
                    break
                mapped = mapped[offset]
            elif isinstance(mapped, dict) and part in mapped:
                mapped = mapped[part]
            else:
                break
        return endpoint

    def visit(value, actual, path):
        if isinstance(value, dict) and isinstance(actual, dict):
            for key, child in value.items():
                if key in actual:
                    visit(child, actual[key], path + [key])
        elif isinstance(value, list) and isinstance(actual, list):
            for index, child in enumerate(value):
                if index < len(actual):
                    visit(child, actual[index], path + [index])
        elif isinstance(value, str):
            match = EXACT_REFERENCE.fullmatch(value)
            if not match or match["root"] == "input" or not match["path"].startswith("output."):
                return
            node = nodes.get(match["root"])
            if not node or node.status != "succeeded" or not node.finished_at:
                return
            try:
                resolved = resolve_value(value, {}, values)
            except (ExpressionError, KeyError, ValueError):
                return
            if digest_json(resolved) == digest_json(actual):
                coverage.append(
                    {
                        "outputPath": path,
                        **trace(node.node_id, match["path"].removeprefix("output."), actual, set()),
                    }
                )

    visit(mapping, output, [])
    return coverage


async def build_execution_receipt(session, invocation):
    if invocation.status != "succeeded" or not invocation.run_id:
        return None
    run = await session.get(Run, invocation.run_id)
    if (
        not run
        or run.status != "succeeded"
        or not run.finished_at
        or run.cancellation_requested_at
        or digest_json(run.output_json) != digest_json(invocation.output_json)
    ):
        return None
    source = await load_execution_source(session, run)
    if not source or invocation.request_json.get("versionId") != source.id:
        return None
    definition = WorkflowDefinition.model_validate(source.definition_json)
    from app.application.schema_defaults import apply_input_defaults

    requested_input = apply_input_defaults(
        invocation.request_json.get("input", {}), definition.spec.input_schema
    )
    if digest_json(requested_input) != digest_json(run.input_json):
        return None
    records = list(
        await session.scalars(
            select(NodeRun).where(NodeRun.run_id == run.id).order_by(NodeRun.attempt)
        )
    )
    latest = {record.node_id: record for record in records}
    if any(n.status not in {"succeeded", "skipped"} for n in latest.values()):
        return None
    output_node = next(n for n in definition.spec.nodes if n.type.value == "output")
    output_record = latest.get(output_node.id)
    if (
        not output_record
        or output_record.status != "succeeded"
        or digest_json(output_record.output_json) != digest_json(run.output_json)
    ):
        return None
    nodes = []
    for node in definition.spec.nodes:
        record = latest.get(node.id)
        if not record or record.status != "succeeded" or not record.finished_at:
            continue
        if record.node_type != node.type.value:
            return None
        item = {
            "id": node.id,
            "type": record.node_type,
            "status": record.status,
            "attempt": record.attempt,
            "completedAt": rfc3339(record.finished_at),
            "outputDigest": digest_json(record.output_json),
        }
        version_id = node.config.get("capabilityVersionId")
        if version_id:
            version = await session.get(CapabilityVersion, version_id)
            if not version:
                return None
            lock = next(
                (
                    lock
                    for lock in source.validation_json.get("capabilityLocks", [])
                    if lock.get("nodeId") == node.id
                ),
                None,
            )
            if source.validation_json.get("capabilityLocks") and not lock:
                return None
            if lock and (
                lock.get("capabilityVersionId", lock.get("versionId")) != version_id
                or lock.get("digest") != version.digest
            ):
                return None
            item.update(capabilityVersionId=version_id, digest=version.digest)
        nodes.append(item)
    coverage = output_coverage(
        output_node.input,
        run.output_json,
        latest,
        {node.id: node for node in definition.spec.nodes},
    )
    return {
        "version": 1,
        "invocationId": invocation.id,
        "runId": run.id,
        "status": "succeeded",
        "completedAt": rfc3339(run.finished_at),
        "source": {"versionId": source.id, "digest": source.digest},
        "inputDigest": digest_json(run.input_json),
        "outputDigest": digest_json(run.output_json),
        "nodes": nodes,
        "coverage": coverage,
    }
