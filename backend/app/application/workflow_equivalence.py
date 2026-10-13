"""Conservative executable equality; semantic reuse is decided before building.

Ignore presentation text and node identifiers, but keep values, defaults, effects,
permissions, retry policies, dependency versions and all executable configuration.
"""

import re
from copy import deepcopy

from sqlalchemy import select

from app.application.compiler import digest_json
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import Workflow, WorkflowCapabilityManifest, WorkflowVersion


def _schema(value):
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    for key in ("title", "description", "$comment", "examples"):
        result.pop(key, None)
    for key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
        if isinstance(result.get(key), dict):
            result[key] = {name: _schema(item) for name, item in result[key].items()}
    for key in (
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
    ):
        if isinstance(result.get(key), dict):
            result[key] = _schema(result[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(key), list):
            result[key] = [_schema(item) for item in result[key]]
    if isinstance(result.get("required"), list):
        result["required"] = sorted(result["required"])
    return result


def workflow_behavior_digest(definition):
    spec = WorkflowDefinition.model_validate(definition).model_dump(mode="json", by_alias=True)[
        "spec"
    ]
    spec.pop("uiSchema", None)
    for key in ("inputSchema", "outputSchema"):
        spec[key] = _schema(spec[key])
    nodes = {node["id"]: node for node in spec["nodes"]}
    predecessors = {id: set() for id in nodes}
    for edge in spec["edges"]:
        predecessors[edge["target"]].add(edge["source"])
    ordered = []
    while len(ordered) < len(nodes):
        ready = sorted(id for id in nodes if id not in ordered and predecessors[id] <= set(ordered))
        if not ready:
            raise ValueError("无法比较循环工作流")
        ordered.extend(ready)
    ids = {id: f"node{index}" for index, id in enumerate(ordered)}

    def references(value):
        if isinstance(value, dict):
            return {k: references(v) for k, v in value.items()}
        if isinstance(value, list):
            return [references(v) for v in value]
        if isinstance(value, str):
            return re.sub(r"\$\{([\w-]+)\.", lambda m: "${" + ids.get(m[1], m[1]) + ".", value)
        return value

    cleaned = []
    for id in ordered:
        node = deepcopy(nodes[id])
        node["id"] = ids[id]
        node.pop("displayName", None)
        # Rebind data expressions only. Prompt text and static config stay exact.
        node["input"] = references(node["input"])
        cleaned.append(node)
    spec["nodes"] = cleaned
    spec["edges"] = sorted(
        [
            {**edge, "source": ids[edge["source"]], "target": ids[edge["target"]]}
            for edge in spec["edges"]
        ],
        key=lambda item: (item["source"], item["target"], str(item.get("when"))),
    )
    # Presentation policies can change output semantics, so retain them.
    return digest_json(
        {
            "spec": spec,
            "presentationPolicy": definition.get("metadata", {}).get("presentationPolicy"),
        }
    )


async def find_equivalent_workflow(session, definition):
    target = workflow_behavior_digest(definition)
    rows = await session.execute(
        select(WorkflowVersion, Workflow)
        .join(Workflow)
        .join(
            WorkflowCapabilityManifest,
            WorkflowCapabilityManifest.workflow_version_id == WorkflowVersion.id,
        )
        .where(WorkflowCapabilityManifest.status.in_(["published", "active"]))
    )
    for version, owner in rows:
        if owner.draft_metadata_json.get("archived"):
            continue
        if workflow_behavior_digest(version.definition_json) == target:
            return version
    return None
