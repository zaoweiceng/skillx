"""Expand pinned workflow calls without losing branch gates or data boundaries."""

from copy import deepcopy

from app.application.compiler import REFERENCE_PATTERN, WorkflowCompiler
from app.infrastructure.database import WorkflowVersion


def rename_refs(value, names):
    if isinstance(value, str):
        return REFERENCE_PATTERN.sub(
            lambda m: "${" + names.get(m["root"], m["root"]) + "." + m["path"] + "}", value
        )
    if isinstance(value, dict):
        return {key: rename_refs(item, names) for key, item in value.items()}
    if isinstance(value, list):
        return [rename_refs(item, names) for item in value]
    return value


async def expand_workflow(session, raw, ancestors=()):
    definition = deepcopy(raw)
    from app.application.callable_capabilities import resolve_callable

    for node in definition["spec"]["nodes"]:
        if node["type"] != "capability_call":
            continue
        config = node.get("config", {})
        if set(config) - {"capabilityVersionId", "interface", "digest"}:
            raise ValueError("能力节点包含未知配置")
        target = await resolve_callable(session, config.get("capabilityVersionId"))
        if config.get("digest") != target.contract["digest"]:
            raise ValueError("能力节点必须锁定正确的 digest")
        kind, arguments = target.bind(config.get("interface", "execute"), node.get("input", {}))
        if kind == "workflow":
            node["type"] = "workflow_call"
            node["config"] = {
                "workflowVersionId": arguments["versionId"], "digest": config["digest"],
            }
        else:
            node["type"] = "skillx"
            node["config"] = {
                "capabilityVersionId": arguments["versionId"], "interface": arguments["interface"],
            }
    calls = [n for n in definition["spec"]["nodes"] if n["type"] == "workflow_call"]
    if not calls:
        return definition
    source_map = dict(definition.get("metadata", {}).get("callSources", {}))
    depth = 0
    for call in calls:
        version_id = call.get("config", {}).get("workflowVersionId")
        if not version_id or version_id in ancestors:
            raise ValueError("子工作流版本缺失或循环引用")
        version = await session.get(WorkflowVersion, version_id)
        if version is None:
            raise ValueError("子工作流版本不存在")
        if call["config"].get("digest") != version.digest:
            raise ValueError("子工作流必须锁定正确的 digest")
        child = await expand_workflow(session, version.definition_json, (*ancestors, version_id))
        child_depth = 1 + int(child.get("metadata", {}).get("nestingDepth", 0))
        depth = max(depth, child_depth)
        if len(ancestors) + depth > 8:
            raise ValueError("工作流嵌套超过 8 层")
        required = child["spec"]["inputSchema"].get("required", [])
        if set(required) - call.get("input", {}).keys():
            raise ValueError("子工作流缺少必填输入映射")
        child_nodes = child["spec"]["nodes"]
        input_node = next(n for n in child_nodes if n["type"] == "input")
        output_node = next(n for n in child_nodes if n["type"] == "output")
        names = {n["id"]: f"{call['id']}__{n['id']}" for n in child_nodes}
        names[output_node["id"]] = call["id"]
        entry = names[input_node["id"]]
        generated = []
        for node in child_nodes:
            item = deepcopy(node)
            item["id"] = names[node["id"]]
            if node["type"] == "input":
                item.update(
                    type="transform",
                    input=call.get("input", {}),
                    config={
                        "operation": "passthrough",
                        "outputSchema": child["spec"]["inputSchema"],
                    },
                )
            else:
                # Workflow's input root has no .output segment.
                def convert(value, entry=entry, names=names):
                    if isinstance(value, str):
                        return REFERENCE_PATTERN.sub(
                            lambda m: (
                                "${"
                                + (
                                    entry + ".output."
                                    if m["root"] == "input"
                                    else names.get(m["root"], m["root"]) + "."
                                )
                                + m["path"]
                                + "}"
                            ),
                            value,
                        )
                    if isinstance(value, dict):
                        return {k: convert(v) for k, v in value.items()}
                    if isinstance(value, list):
                        return [convert(v) for v in value]
                    return value

                item["input"] = convert(item.get("input", {}))
                if node["type"] == "output":
                    item.update(
                        type="transform",
                        config={
                            "operation": "passthrough",
                            "outputSchema": child["spec"]["outputSchema"],
                        },
                    )
            if item.get("fallback"):
                item["fallback"]["target"] = names[item["fallback"]["target"]]
            source_map[item["id"]] = {
                "workflowVersionId": version_id,
                "digest": version.digest,
                "nodeId": node["id"],
                "parentCall": call["id"],
            }
            generated.append(item)
        edges = [
            {**e, "target": entry if e["target"] == call["id"] else e["target"]}
            for e in definition["spec"]["edges"]
        ]
        edges.extend(
            {**e, "source": names[e["source"]], "target": names[e["target"]]}
            for e in child["spec"]["edges"]
        )
        definition["spec"]["nodes"] = [
            n for n in definition["spec"]["nodes"] if n["id"] != call["id"]
        ] + generated
        definition["spec"]["edges"] = edges
    definition.setdefault("metadata", {}).update(callSources=source_map, nestingDepth=depth)
    WorkflowCompiler().compile(definition)
    return definition
