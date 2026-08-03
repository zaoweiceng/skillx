from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.infrastructure.database import ConversationRun, Distillation, Workflow

STEP_ID = re.compile(r"[^a-zA-Z0-9_-]+")


class DistillationError(ValueError):
    pass


def compile_trajectory(trajectory: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    steps = trajectory.get("steps")
    if not isinstance(steps, list) or not steps:
        raise DistillationError("轨迹必须包含至少一个结构化 step")
    nodes: list[dict[str, Any]] = [{"id": "input", "type": "input", "displayName": "输入"}]
    edges: list[dict[str, str]] = []
    previous = "input"
    diagnostics: list[dict[str, str]] = []
    llm_nodes = 0
    for index, step in enumerate(steps, start=1):
        kind = step.get("kind")
        node_id = STEP_ID.sub("-", step.get("id", f"step-{index}")).strip("-")
        if not node_id or node_id in {item["id"] for item in nodes}:
            raise DistillationError(f"轨迹 step {index} 的 id 无效或重复")
        node = {
            "id": node_id,
            "type": kind,
            "displayName": step.get("displayName", node_id),
            "input": step.get("input", {}),
            "config": step.get("config", {}),
        }
        if kind not in {"transform", "mcp", "skillx", "llm", "agent", "artifact"}:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "UNSTABLE_STEP",
                    "path": f"steps.{index - 1}",
                    "message": f"步骤 {node_id} 无法确定化，需显式改为受限 Agent",
                }
            )
            node["type"] = "agent"
            node["config"] = {
                "outputSchema": step.get("outputSchema", {"type": "object"}),
                "maxSteps": step.get("maxSteps", 6),
                "maxTotalTokens": step.get("maxTotalTokens", 30000),
                "allowedCapabilities": step.get("allowedCapabilities", []),
                "prompt": step.get("summary", "完成该动态步骤"),
            }
        if node["type"] in {"llm", "agent"}:
            llm_nodes += 1
        nodes.append(node)
        edges.append({"source": previous, "target": node_id})
        previous = node_id
    output_mapping = trajectory.get("outputMapping")
    if not isinstance(output_mapping, dict):
        raise DistillationError("轨迹缺少显式 outputMapping")
    nodes.append(
        {
            "id": "output",
            "type": "output",
            "displayName": "输出",
            "input": output_mapping,
        }
    )
    edges.append({"source": previous, "target": "output"})
    definition = {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": trajectory.get("name", "distilled-workflow")},
        "spec": {
            "inputSchema": trajectory["inputSchema"],
            "uiSchema": trajectory.get("uiSchema", {}),
            "outputSchema": trajectory["outputSchema"],
            "nodes": nodes,
            "edges": edges,
            "policies": trajectory.get(
                "policies",
                {"maxDurationSeconds": 300, "maxParallelNodes": 2},
            ),
        },
    }
    try:
        WorkflowCompiler().compile(definition)
    except WorkflowValidationError as exc:
        diagnostics.extend(item.model_dump() for item in exc.diagnostics)
    analysis = {
        "directNodes": len(nodes) - llm_nodes - 2,
        "llmOrAgentNodes": llm_nodes,
        "estimatedLlmCallsBefore": trajectory.get("originalModelCalls", 0),
        "estimatedLlmCallsAfter": llm_nodes,
        "diagnostics": diagnostics,
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }
    return definition, analysis


async def distill_conversation_run(
    session: AsyncSession,
    conversation_run: ConversationRun,
) -> Distillation:
    if conversation_run.status != "succeeded":
        raise DistillationError("只有成功的 Agent Run 可以固化")
    definition, analysis = compile_trajectory(conversation_run.trajectory_json)
    workflow = Workflow(
        id=str(uuid4()),
        name=definition["metadata"]["name"],
        owner_id="local-user",
        draft_revision=1,
        draft_json=definition,
    )
    session.add(workflow)
    await session.flush()
    result = Distillation(
        id=str(uuid4()),
        conversation_run_id=conversation_run.id,
        status="draft_created",
        draft_workflow_id=workflow.id,
        analysis_json=analysis,
    )
    session.add(result)
    await session.flush()
    return result
