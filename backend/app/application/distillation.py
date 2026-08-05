from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.action_compilers import ActionCompilerRegistry, CompileContext
from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.compiler import (
    WorkflowCompiler,
    WorkflowValidationError,
    digest_json,
)
from app.application.distillation_ir import (
    DistillationProposal,
    NormalizedAction,
    TrajectoryAnalysisError,
    build_action_ir,
)
from app.config import get_settings
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
) -> dict[str, tuple[str, str]]:
    """校验 Agent 的 DistillationProposal：必须覆盖每个 action 恰好一次。

    返回 actionId → proposed_strategy 的映射，供 Compiler 参与裁决（Compiler 仍是最终
    裁决者：建议不合法时抛 DistillationError，不静默换成错误图）。
    """

    action_ids = [action.action_id for action in actions]
    covered: list[str] = []
    assignments: dict[str, tuple[str, str]] = {}
    node_ids: set[str] = set()
    for proposed in proposal.nodes:
        if proposed.node_id in node_ids:
            raise DistillationError(f"DistillationProposal 节点 id 重复: {proposed.node_id}")
        node_ids.add(proposed.node_id)
        positions = sorted(
            action_ids.index(action_id)
            for action_id in proposed.action_ids
            if action_id in action_ids
        )
        if positions and positions != list(range(positions[0], positions[-1] + 1)):
            raise DistillationError("ProposedNode action group 必须在轨迹中连续")
        group_ids = set(proposed.action_ids)
        for action_id in proposed.action_ids:
            action = next((item for item in actions if item.action_id == action_id), None)
            if action is None:
                continue
            for dependency in action.depends_on:
                if (
                    dependency not in group_ids
                    and action_ids.index(dependency) >= positions[0]
                ):
                    raise DistillationError("ProposedNode action group 不是拓扑闭合分组")
        if proposed.strategy == "deterministic" and len(proposed.action_ids) != 1:
            raise DistillationError("deterministic ProposedNode 必须恰好包含一个 action")
        for action_id in proposed.action_ids:
            covered.append(action_id)
            assignments[action_id] = (proposed.node_id, proposed.strategy)
    if sorted(covered) != sorted(action_ids):
        missing = set(action_ids) - set(covered)
        extra = set(covered) - set(action_ids)
        raise DistillationError(
            f"DistillationProposal 未恰好覆盖每个动作：缺失 {sorted(missing)}，多余 {sorted(extra)}"
        )
    if len(covered) != len(set(covered)):
        raise DistillationError("DistillationProposal 存在重复覆盖的动作")
    return assignments


def _grouped_action(
    group: list[NormalizedAction],
    strategy: str,
    output_schema: dict[str, Any],
) -> NormalizedAction:
    """把 ProposedNode 中多个动作收敛为一个概率性边界节点。"""

    group_ids = {action.action_id for action in group}
    bindings = []
    seen_fields: set[str] = set()
    for action in group:
        for binding in action.input_bindings:
            if binding.from_action in group_ids:
                continue
            field = binding.field
            if field in seen_fields:
                field = f"{action.action_id}_{field}"
            seen_fields.add(field)
            bindings.append(binding.model_copy(update={"field": field}))
    capabilities = sorted(
        {
            action.capability_ref.name
            for action in group
            if action.capability_ref is not None
        }
    )
    capability_locks = [
        {
            "registryId": action.capability_ref.registry_id,
            "name": action.capability_ref.name,
            "version": action.capability_ref.version,
            "digest": action.capability_ref.digest,
        }
        for action in group
        if action.capability_ref is not None
    ]
    external_dependencies = sorted(
        {
            dependency
            for action in group
            for dependency in action.depends_on
            if dependency not in group_ids
        }
    )
    last = group[-1]
    return last.model_copy(
        update={
            "kind": strategy,
            "input_bindings": bindings,
            "output_schema": last.output_schema or output_schema,
            "depends_on": external_dependencies,
            "observations": {
                **last.observations,
                "allowedCapabilities": capabilities,
                "capabilityLocks": capability_locks,
                "prompt": (
                    f"按原顺序复现动作组 {[action.action_id for action in group]}，"
                    "保持边界输入输出契约。"
                ),
            },
        }
    )


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

    proposed_assignments: dict[str, tuple[str, str]] = {}
    if proposal is not None:
        proposed_assignments = _validate_proposal(proposal, actions)

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
    emitted_groups: dict[str, str] = {}
    last_node = "input"
    last_output_ref = "${input.message}"

    for action in actions:
        cap_name = action.capability_ref.name if action.capability_ref else action.kind
        proposed_node_id, proposed_strategy = proposed_assignments.get(
            action.action_id, (f"{action.kind}-{cap_name}", "deterministic")
        )
        if proposed_node_id in emitted_groups:
            action_node_ids[action.action_id] = emitted_groups[proposed_node_id]
            continue
        group = (
            [
                item
                for item in actions
                if proposed_assignments.get(item.action_id, (None, None))[0]
                == proposed_node_id
            ]
            if proposal is not None
            else [action]
        )
        compiled_action = (
            _grouped_action(group, proposed_strategy, output_schema)
            if len(group) > 1
            else action
        )
        node_id = _node_id(proposed_node_id, taken)
        ctx = CompileContext(
            node_id=node_id,
            message=trajectory["prompt"],
            output_schema=output_schema,
            action_node_ids=action_node_ids,
            proposed_strategy=proposed_strategy,
        )
        # proposed_strategy=agent：Agent 显式建议保留概率性节点，直接编译为 Agent 节点，
        # 不尝试确定化（Agent 的判断优先于默认 heuristic）。仍受能力锁与预算约束。
        if ctx.proposed_strategy in {"agent", "llm"}:
            result = registry.compile_as(ctx.proposed_strategy, compiled_action, ctx)
        else:
            result = registry.compile(compiled_action, ctx)
        unknown_bindings = [
            binding.field
            for grouped in group
            for binding in grouped.input_bindings
            if binding.source == "unknown"
        ]
        if unknown_bindings:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "MISSING_ARGUMENT_PROVENANCE",
                    "path": action.action_id,
                    "message": f"参数缺少 provenance: {sorted(unknown_bindings)}",
                }
            )
        if result.decision == "rejected":
            diagnostics.extend(result.diagnostics)
            # 被拒绝的动作仍需保留位置：退回显式 Agent 节点，避免丢步骤，但草稿不可发布。
            fallback = registry.compile_as("agent", compiled_action, ctx)
            node = fallback.node
        else:
            node = result.node
            if result.decision == "compiled":
                for grouped_action in group:
                    ref = grouped_action.capability_ref
                    if ref and ref.digest:
                        capability_locks.append(
                            {
                                "nodeId": node_id,
                                "kind": grouped_action.kind,
                                "capability": ref.name,
                                "registryId": ref.registry_id,
                                "version": ref.version,
                                "digest": ref.digest,
                            }
                        )
        assert node is not None
        # 按结果节点类型统计：agent/llm 计入模型节点，其余确定节点计入 direct。
        if node["type"] in ("agent", "llm"):
            agent_nodes += 1
        else:
            deterministic_nodes += 1
        nodes.append(node)
        emitted_groups[proposed_node_id] = node_id
        for grouped in group:
            action_node_ids[grouped.action_id] = node_id
        # 真实数据依赖边：从该动作的 dependsOn 连接上游节点；无上游则从 input 连入。
        upstream_nodes = [
            action_node_ids[dep]
            for dep in compiled_action.depends_on
            if dep in action_node_ids and action_node_ids[dep] != node_id
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
    output_mapping, output_diagnostics = _build_output_mapping(
        output_schema,
        trajectory.get("outputProvenance"),
        action_node_ids,
        last_node,
        nodes,
        last_output_ref,
    )
    diagnostics.extend(output_diagnostics)
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


def _enrich_distilled_definition(
    definition: dict[str, Any],
    trajectory: dict[str, Any],
    *,
    conversation_run_id: str,
    distillation_id: str,
    source_trajectory_digest: str,
    proposal_digest: str | None,
) -> dict[str, Any]:
    """派生可发现、可拆取、可恢复且带完整来源的固化 metadata。"""

    prompt = _clean_message(str(trajectory.get("prompt", ""))).strip()
    actions = list(trajectory.get("actions") or [])
    action_kinds = sorted(
        {
            str(action.get("kind") or "agent")
            for action in actions
            if isinstance(action, dict)
        }
    )
    semantic_seed = prompt or "已验证的 Agent 轨迹"
    logical_id = digest_json(
        {
            "inputSchema": definition["spec"]["inputSchema"],
            "outputSchema": definition["spec"]["outputSchema"],
            "kinds": action_kinds,
            "summary": semantic_seed[:200],
        }
    )
    display_name = semantic_seed[:80]
    if trajectory.get("name") not in {None, "", "conversation-agent-draft"}:
        display_name = str(trajectory["name"])[:80]
    definition["metadata"] = {
        **definition.get("metadata", {}),
        "name": display_name,
        "logicalWorkflowId": logical_id,
        "capability": {
            "name": display_name,
            "summary": f"复用已验证轨迹完成：{semantic_seed[:240]}",
            "useWhen": [semantic_seed[:300]],
            "doNotUseWhen": ["输入不满足冻结 Schema 或需要扩大副作用权限"],
            "tags": ["distilled", *action_kinds][:12],
            "examples": [semantic_seed[:300]],
        },
        "lineage": {
            "sourceConversationRunId": conversation_run_id,
            "sourceTrajectoryDigest": source_trajectory_digest,
            "distillationId": distillation_id,
            "proposalDigest": proposal_digest,
        },
    }

    nodes = definition["spec"]["nodes"]
    recoverable = [
        node
        for node in nodes
        if node["type"] in {"browser", "mcp", "skillx"}
        and not node.get("sideEffect", False)
    ]
    recovery_id = "recovery-agent"
    if recoverable and not any(node["id"] == recovery_id for node in nodes):
        nodes.insert(
            -1,
            {
                "id": recovery_id,
                "type": "agent",
                "displayName": "受限恢复 Agent",
                "input": {},
                "config": {
                    "maxSteps": 4,
                    "maxTotalTokens": 12000,
                    "maxDurationSeconds": 60,
                    "allowedCapabilities": [],
                    "outputSchema": {"type": "object"},
                },
            },
        )
        for node in recoverable:
            node["fallback"] = {
                "on": [
                    "DEPENDENCY_UNAVAILABLE",
                    "TIMEOUT",
                    "PAGE_DRIFT",
                    "MCP_CONTRACT_DRIFT",
                ],
                "target": recovery_id,
                "requireUserApproval": False,
            }

    business_nodes = [
        node
        for node in nodes
        if node["type"] not in {"input", "output"}
    ]
    definition["metadata"]["nodeSummaries"] = [
        {
            "nodeId": node["id"],
            "summary": node["displayName"],
            "type": node["type"],
        }
        for node in business_nodes
        if node["id"] != recovery_id
    ]
    if business_nodes and not any(node.get("sideEffect", False) for node in business_nodes):
        definition["metadata"]["callableFragments"] = [
            {
                "name": "verified-task",
                "summary": f"复用任务中的已验证动作：{semantic_seed[:160]}",
                "useWhen": [semantic_seed[:240]],
                "nodeIds": [node["id"] for node in business_nodes],
            }
        ]
    return definition


def _node_output_ref(node_id: str, node: dict[str, Any]) -> str:
    """给定节点，返回可映射到 workflow output 的引用表达式。"""

    if node["type"] == "transform" and node.get("config", {}).get("operation") == "lookup":
        return f"${{{node_id}.output.value}}"
    return f"${{{node_id}.output}}"


def _build_output_mapping(
    output_schema: dict[str, Any],
    output_provenance: Any,
    action_node_ids: dict[str, str],
    last_node: str,
    nodes: list[dict[str, Any]],
    last_output_ref: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """把最后一个节点的输出映射到 workflow output 的每个属性。

    - lookup 节点：单 scalar value 映射到唯一输出属性；
    - 结构化输出节点（agent/llm）：按属性名逐字段映射 ${node.output.<field>}。
    """

    properties = list(output_schema.get("properties", {}))
    diagnostics: list[dict[str, str]] = []
    mapping: dict[str, Any] = {}
    provenance = output_provenance if isinstance(output_provenance, dict) else {}
    for key in properties:
        source = provenance.get(key)
        if not isinstance(source, dict):
            diagnostics.append(
                {
                    "level": "error",
                    "code": "MISSING_OUTPUT_PROVENANCE",
                    "path": f"output.{key}",
                    "message": f"Workflow 输出字段 {key} 缺少显式 provenance",
                }
            )
            continue
        action_id = source.get("actionId") or source.get("lastActionId")
        node_id = action_node_ids.get(str(action_id))
        if node_id is None:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "INVALID_OUTPUT_PROVENANCE",
                    "path": f"output.{key}",
                    "message": f"输出字段 {key} 引用了未知 action {action_id}",
                }
            )
            continue
        path = source.get("path", key)
        mapping[key] = f"${{{node_id}.output.{path}}}"
    if mapping or diagnostics:
        return mapping, diagnostics
    node = next((item for item in nodes if item["id"] == last_node), None)
    node_type = node["type"] if node else "agent"
    if node_type in ("agent", "llm"):
        return (
            {key: f"${{{last_node}.output.{key}}}" for key in properties},
            diagnostics,
        )
    # 确定节点（如 lookup）：所有输出属性映射到该节点的 scalar 结果引用。
    return {key: last_output_ref for key in properties}, diagnostics


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
    proposal_source = "runtime" if isinstance(raw_proposal, dict) else "none"
    if isinstance(raw_proposal, dict):
        try:
            proposal = DistillationProposal.model_validate(raw_proposal)
        except Exception as exc:  # noqa: BLE001 - 非法提议按固化失败返回 422。
            raise DistillationError(f"非法 DistillationProposal: {exc}") from exc
    elif get_settings().traex_command:
        proposal = await _request_distillation_proposal(
            conversation_run.id, conversation_run.trajectory_json
        )
        proposal_source = "distillation_agent" if proposal is not None else "none"
    definition, analysis = compile_agent_trajectory(
        conversation_run.trajectory_json, proposal
    )
    source_trajectory_digest = digest_json(conversation_run.trajectory_json)
    proposal_digest = (
        digest_json(proposal.model_dump(by_alias=True)) if proposal else None
    )
    distillation_id = str(uuid4())
    definition = _enrich_distilled_definition(
        definition,
        conversation_run.trajectory_json,
        conversation_run_id=conversation_run.id,
        distillation_id=distillation_id,
        source_trajectory_digest=source_trajectory_digest,
        proposal_digest=proposal_digest,
    )
    try:
        compiled = WorkflowCompiler().compile(definition)
        enrichment_diagnostics = [
            item.model_dump() for item in compiled.diagnostics
        ]
    except WorkflowValidationError as exc:
        enrichment_diagnostics = [
            item.model_dump() for item in exc.diagnostics
        ]
    analysis["diagnostics"] = [
        *analysis.get("diagnostics", []),
        *enrichment_diagnostics,
    ]
    analysis["publishable"] = not any(
        item["level"] == "error" for item in analysis["diagnostics"]
    )
    analysis = {
        **analysis,
        "sourceRunId": conversation_run.id,
        "sourceRunType": run_type,
        "usedProposal": proposal is not None,
        "proposalSource": proposal_source,
        "sourceTrajectoryDigest": source_trajectory_digest,
        "proposalDigest": proposal_digest,
        "logicalWorkflowId": definition["metadata"]["logicalWorkflowId"],
        "fragmentProposals": definition["metadata"].get("callableFragments", []),
        "recoverableNodes": [
            node["id"]
            for node in definition["spec"]["nodes"]
            if node.get("fallback")
        ],
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
        id=distillation_id,
        conversation_run_id=conversation_run.id,
        status="draft_created",
        draft_workflow_id=workflow.id,
        analysis_json=analysis,
    )
    session.add(result)
    await session.flush()
    return result


async def _request_distillation_proposal(
    run_id: str, trajectory: dict[str, Any]
) -> DistillationProposal | None:
    """零工具受限阶段：只对动作摘要提出分组，不执行原任务。"""

    settings = get_settings()
    action_summary = [
        {
            "actionId": action.get("callId"),
            "kind": action.get("kind"),
            "capability": action.get("capability") or action.get("tool"),
            "bindings": action.get("argumentBindings", {}),
            "outputSchema": action.get("outputSchema"),
        }
        for action in trajectory.get("actions", [])
    ]
    request = AgentHostRequest(
        run_id=f"distillation-{run_id}",
        prompt=(
            "你是受限 Distillation Agent。只根据动作摘要提出节点分组、策略和变量候选；"
            "每个 action 必须恰好归属一个节点，不得调用工具。\n"
            f"actions={action_summary}"
        ),
        output_schema=DistillationProposal.model_json_schema(by_alias=True),
        working_directory=settings.artifact_root / "distillation" / run_id,
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=8000,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        return DistillationProposal.model_validate(result.output)
    except (AgentHostError, ValueError):
        # Runtime 不支持该 Schema 时不伪造 Agent 提议；Compiler 仍可生成带阻断诊断的草稿。
        return None
