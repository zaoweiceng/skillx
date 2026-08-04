from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.action_compilers import ActionCompilerRegistry, CompileContext
from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.distillation_ir import (
    TrajectoryAnalysisError,
    build_action_ir,
)
from app.infrastructure.database import ConversationRun, Distillation, Workflow

STEP_ID = re.compile(r"[^a-zA-Z0-9_-]+")
DIRECTIVE = re.compile(r"\[\[.*?\]\]")


class DistillationError(ValueError):
    pass


def _clean_message(prompt: str) -> str:
    """去除 fixture 指令 ``[[...]]`` 后的用户语义输入，用于参数化匹配。"""

    return DIRECTIVE.sub("", prompt).strip()


def _node_id(base: str, taken: set[str]) -> str:
    node_id = STEP_ID.sub("-", base).strip("-").lower() or "node"
    candidate = node_id
    index = 1
    while candidate in taken:
        index += 1
        candidate = f"{node_id}-{index}"
    taken.add(candidate)
    return candidate


def compile_agent_trajectory(
    trajectory: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从真实 Agent 轨迹的成功 action 序列提炼混合工作流。

    流程：NormalizedAction IR → Action Compiler Registry 逐个裁决 → 保留顺序与数据依赖
    生成“确定前缀 + 局部 Agent/LLM + 确定后缀”的混合图。绝不因为存在一个确定节点就
    丢弃其它动作；未识别动作保留为显式 Agent 节点，被拒绝的确定化建议产生 error 级
    diagnostic 使草稿不可发布。
    """

    input_schema = trajectory.get("inputSchema")
    output_schema = trajectory.get("outputSchema")
    if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
        raise DistillationError("轨迹缺少 inputSchema/outputSchema")
    trajectory = {**trajectory, "prompt": _clean_message(str(trajectory.get("prompt", "")))}

    try:
        actions = build_action_ir(trajectory)
    except TrajectoryAnalysisError as exc:
        raise DistillationError(str(exc)) from exc

    registry = ActionCompilerRegistry()
    nodes: list[dict[str, Any]] = [{"id": "input", "type": "input", "displayName": "输入"}]
    edges: list[dict[str, str]] = []
    taken = {"input", "output"}
    previous = "input"
    diagnostics: list[dict[str, str]] = []
    deterministic_nodes = 0
    agent_nodes = 0
    capability_locks: list[dict[str, Any]] = []
    last_node = "input"
    last_output_ref = "${input.message}"

    for action in actions:
        cap_name = action.capability_ref.name if action.capability_ref else action.kind
        node_id = _node_id(f"{action.kind}-{cap_name}", taken)
        ctx = CompileContext(
            node_id=node_id, message=trajectory["prompt"], output_schema=output_schema
        )
        result = registry.compile(action, ctx)
        if result.decision == "rejected":
            diagnostics.extend(result.diagnostics)
            # 被拒绝的动作仍需保留位置：退回显式 Agent 节点，避免丢步骤，但草稿不可发布。
            fallback = registry._compilers["agent"].compile(action, ctx)  # noqa: SLF001
            node = fallback.node
        else:
            node = result.node
            if result.decision == "compiled" and action.capability_ref and (
                action.capability_ref.digest
            ):
                capability_locks.append(
                    {
                        "nodeId": node_id,
                        "kind": action.kind,
                        "capability": action.capability_ref.name,
                        "registryId": action.capability_ref.registry_id,
                        "version": action.capability_ref.version,
                        "digest": action.capability_ref.digest,
                    }
                )
        assert node is not None
        # 按结果节点类型统计：agent/llm 计入模型节点，其余确定节点计入 direct。
        if node["type"] in ("agent", "llm"):
            agent_nodes += 1
        else:
            deterministic_nodes += 1
        nodes.append(node)
        edges.append({"source": previous, "target": node_id})
        previous = node_id
        last_node = node_id
        # 输出引用：确定节点用其结构化输出，Agent/LLM 节点用 output 字段。
        last_output_ref = _node_output_ref(node_id, node)

    if not actions:
        raise DistillationError("轨迹不含任何可分析动作")

    output_mapping = _build_output_mapping(output_schema, last_node, nodes, last_output_ref)
    nodes.append(
        {"id": "output", "type": "output", "displayName": "输出", "input": output_mapping}
    )
    edges.append({"source": previous, "target": "output"})

    definition = {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": trajectory.get("name", "distilled-workflow")},
        "spec": {
            "inputSchema": input_schema,
            "uiSchema": trajectory.get("uiSchema", {}),
            "outputSchema": output_schema,
            "nodes": nodes,
            "edges": edges,
            "policies": trajectory.get(
                "policies", {"maxDurationSeconds": 300, "maxParallelNodes": 2}
            ),
        },
    }
    try:
        WorkflowCompiler().compile(definition)
    except WorkflowValidationError as exc:
        diagnostics.extend(item.model_dump() for item in exc.diagnostics)
    analysis = {
        "directNodes": deterministic_nodes,
        "agentNodes": agent_nodes,
        "llmOrAgentNodes": agent_nodes,
        "estimatedLlmCallsBefore": trajectory.get("originalModelCalls", 0),
        "estimatedLlmCallsAfter": agent_nodes,
        "capabilityLocks": capability_locks,
        "unsupportedActions": [
            action.action_id for action in actions if action.replayability == "unsupported"
        ],
        "diagnostics": diagnostics,
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }
    return definition, analysis


def _node_output_ref(node_id: str, node: dict[str, Any]) -> str:
    """给定节点，返回可映射到 workflow output 的引用表达式。"""

    if node["type"] == "transform" and node.get("config", {}).get("operation") == "lookup":
        return f"${{{node_id}.output.value}}"
    return f"${{{node_id}.output}}"


def _build_output_mapping(
    output_schema: dict[str, Any],
    last_node: str,
    nodes: list[dict[str, Any]],
    last_output_ref: str,
) -> dict[str, Any]:
    """把最后一个节点的输出映射到 workflow output 的每个属性。

    - lookup 节点：单 scalar value 映射到唯一输出属性；
    - 结构化输出节点（agent/llm）：按属性名逐字段映射 ${node.output.<field>}。
    """

    properties = list(output_schema.get("properties", {}))
    node = next((item for item in nodes if item["id"] == last_node), None)
    node_type = node["type"] if node else "agent"
    if node_type in ("agent", "llm"):
        return {key: f"${{{last_node}.output.{key}}}" for key in properties}
    # 确定节点（如 lookup）：所有输出属性映射到该节点的 scalar 结果引用。
    return {key: last_output_ref for key in properties}


async def distill_conversation_run(
    session: AsyncSession,
    conversation_run: ConversationRun,
) -> Distillation:
    if conversation_run.status != "succeeded":
        raise DistillationError("只有成功的运行可以固化")
    # 默认只固化 AgentRunService 生成的成功 agent_run：这是“探索一次，固化复用”的
    # 唯一生产者。外部导入轨迹与单次 llm_response 都不作为固化来源。
    run_type = getattr(conversation_run, "run_type", "agent_run")
    if run_type != "agent_run":
        raise DistillationError(
            "只有服务端生成的 agent_run 可固化；单次回答或导入轨迹请勿作为固化来源"
        )
    definition, analysis = compile_agent_trajectory(conversation_run.trajectory_json)
    analysis = {
        **analysis,
        "sourceRunId": conversation_run.id,
        "sourceRunType": run_type,
    }
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
