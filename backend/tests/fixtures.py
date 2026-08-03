from typing import Any


def deterministic_workflow(with_approval: bool = False) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = [
        {
            "id": "input",
            "type": "input",
            "displayName": "输入",
        },
        {
            "id": "format",
            "type": "transform",
            "displayName": "格式化",
            "input": {"name": "${input.name}"},
            "config": {"operation": "template", "template": "你好，{name}"},
        },
    ]
    edges = [{"source": "input", "target": "format"}]
    previous = "format"
    if with_approval:
        nodes.append(
            {
                "id": "approval",
                "type": "approval",
                "displayName": "确认输出",
                "input": {"message": "${format.output.text}"},
                "config": {
                    "description": "确认后继续",
                    "effect": "data.write",
                },
                "sideEffect": True,
                "idempotent": False,
            }
        )
        edges.append({"source": "format", "target": "approval"})
        previous = "approval"
        output_input = {
            "message": "${approval.output.snapshot.message}",
            "approved": "${approval.output.approved}",
        }
        output_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["message", "approved"],
            "properties": {
                "message": {"type": "string"},
                "approved": {"type": "boolean"},
            },
            "additionalProperties": False,
        }
    else:
        output_input = {"message": "${format.output.text}"}
        output_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
            "additionalProperties": False,
        }
    nodes.append(
        {
            "id": "output",
            "type": "output",
            "displayName": "输出",
            "input": output_input,
        }
    )
    edges.append({"source": previous, "target": "output"})
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "greeting"},
        "spec": {
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "outputSchema": output_schema,
            "nodes": nodes,
            "edges": edges,
            "policies": {"maxDurationSeconds": 60, "maxParallelNodes": 2},
        },
    }
