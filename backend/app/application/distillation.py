from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.action_compilers import ActionCompilerRegistry, CompileContext
from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.distillation_ir import (
    DistillationProposal,
    NormalizedAction,
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


def _validate_proposal(
    proposal: DistillationProposal,
    actions: list[NormalizedAction],
) -> dict[str, str]:
    """校验 Agent 的 DistillationProposal：必须覆盖每个 action 恰好一次。

    返回 actionId → proposed_strategy 的映射，供 Compiler 参与裁决（Compiler 仍是最终
    裁决者：建议不合法时抛 DistillationError，不静默换成错误图）。
    """

    action_ids = [action.action_id for action in actions]
    covered: list[str] = []
    strategies: dict[str, str] = {}
    for proposed in proposal.nodes:
        for action_id in proposed.action_ids:
            covered.append(action_id)
            strategies[action_id] = proposed.strategy
    if sorted(covered) != sorted(action_ids):
        missing = set(action_ids) - set(covered)
        extra = set(covered) - set(action_ids)
        raise DistillationError(
            f"DistillationProposal 未恰好覆盖每个动作：缺失 {sorted(missing)}，多余 {sorted(extra)}"
        )
    if len(covered) != len(set(covered)):
        raise DistillationError("DistillationProposal 存在重复覆盖的动作")
    return strategies


def compile_agent_trajectory(
    trajectory: dict[str, Any],
    proposal: DistillationProposal | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从真实 Agent 轨迹的成功 action 序列提炼混合工作流。

    流程：NormalizedAction IR →（可选）校验 TraeX 的 DistillationProposal 覆盖 →
    Action Compiler Registry 逐个裁决 → 保留顺序与数据依赖生成“确定前缀 + 局部
    Agent/LLM + 确定后缀”的混合图。绝不因为存在一个确定节点就丢弃其它动作；未识别动作
    保留为显式 Agent 节点，被拒绝的确定化建议产生 error 级 diagnostic 使草稿不可发布。
    Agent 只能建议 proposed_strategy；Compiler 是最终裁决者。
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

    proposed_strategies: dict[str, str] = {}
    if proposal is not None:
        proposed_strategies = _validate_proposal(proposal, actions)

    registry = ActionCompilerRegistry()
    nodes: list[dict[str, Any]] = [{"id": "input", "type": "input", "displayName": "输入"}]
    edges: list[dict[str, str]] = []
    taken = {"input", "output"}
    diagnostics: list[dict[str, str]] = []
    deterministic_nodes = 0
    agent_nodes = 0
    capability_locks: list[dict[str, Any]] = []
    # actionId → 编译出的节点 id：把 IR 的数据依赖翻译为真实节点间引用与边。
    action_node_ids: dict[str, str] = {}
    last_node = "input"
    last_output_ref = "${input.message}"

    for action in actions:
        cap_name = action.capability_ref.name if action.capability_ref else action.kind
        node_id = _node_id(f"{action.kind}-{cap_name}", taken)
        ctx = CompileContext(
            node_id=node_id,
            message=trajectory["prompt"],
            output_schema=output_schema,
            action_node_ids=action_node_ids,
            proposed_strategy=proposed_strategies.get(action.action_id, "deterministic"),
        )
        # proposed_strategy=agent：Agent 显式建议保留概率性节点，直接编译为 Agent 节点，
        # 不尝试确定化（Agent 的判断优先于默认 heuristic）。仍受能力锁与预算约束。
        if ctx.proposed_strategy == "agent":
            result = registry._compilers["agent"].compile(action, ctx)  # noqa: SLF001
        else:
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
        action_node_ids[action.action_id] = node_id
        # 真实数据依赖边：从该动作的 dependsOn 连接上游节点；无上游则从 input 连入。
        upstream_nodes = [
            action_node_ids[dep] for dep in action.depends_on if dep in action_node_ids
        ]
        if not upstream_nodes:
            upstream_nodes = ["input"]
        for source in upstream_nodes:
            edges.append({"source": source, "target": node_id})
        last_node = node_id
        # 输出引用：确定节点用其结构化输出，Agent/LLM 节点用 output 字段。
        last_output_ref = _node_output_ref(node_id, node)

    if not actions:
        raise DistillationError("轨迹不含任何可分析动作")

    # 找出所有没有下游的“叶子”节点（终端），连到 output。
    sources_with_downstream = {edge["source"] for edge in edges}
    leaf_nodes = [
        node["id"]
        for node in nodes
        if node["id"] not in sources_with_downstream and node["id"] != "output"
    ]
    output_mapping = _build_output_mapping(output_schema, last_node, nodes, last_output_ref)
    nodes.append(
        {"id": "output", "type": "output", "displayName": "输出", "input": output_mapping}
    )
    for leaf in leaf_nodes:
        edges.append({"source": leaf, "target": "output"})

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
    # 若轨迹携带 TraeX 的 DistillationProposal，则校验其覆盖并让 proposed_strategy 参与裁决。
    proposal: DistillationProposal | None = None
    raw_proposal = conversation_run.trajectory_json.get("proposal")
    if isinstance(raw_proposal, dict):
        try:
            proposal = DistillationProposal.model_validate(raw_proposal)
        except Exception as exc:  # noqa: BLE001 - 非法提议按固化失败返回 422。
            raise DistillationError(f"非法 DistillationProposal: {exc}") from exc
    definition, analysis = compile_agent_trajectory(
        conversation_run.trajectory_json, proposal
    )
    analysis = {
        **analysis,
        "sourceRunId": conversation_run.id,
        "sourceRunType": run_type,
        "usedProposal": proposal is not None,
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
