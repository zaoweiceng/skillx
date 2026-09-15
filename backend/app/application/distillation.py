from __future__ import annotations

import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    attempt_diagnostics,
    build_action_ir,
)
from app.application.runtime_config import agent_runtime_config
from app.config import get_settings
from app.infrastructure.database import ConversationRun, Distillation, Workflow

STEP_ID = re.compile(r"[^a-zA-Z0-9_-]+")
DIRECTIVE = re.compile(r"\[\[.*?\]\]")
_URL = re.compile(r"https?://[^\s]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _deinstantiate_intent(trajectory: dict[str, Any]) -> dict[str, str]:
    """从源任务提取去实例化的任务意图与源语言。

    去掉 fixture 指令、URL 以及每个 user_input 绑定的具体实例值（如业务键、路径），
    保留源语言的通用意图句子。具体 URL、key、Secret、ToolResult 正文都不进入结果，
    因此可以安全写入 manifest 的可检索语料并交给 Descriptor 阶段做同语言描述。
    """

    prompt = _clean_message(str(trajectory.get("prompt", "")))
    text = _URL.sub(" ", prompt)
    instance_values: set[str] = set()
    for action in trajectory.get("actions", []):
        if not isinstance(action, dict):
            continue
        arguments = action.get("arguments") or {}
        bindings = action.get("argumentBindings") or {}
        for field, binding in bindings.items():
            if isinstance(binding, dict) and binding.get("source") == "user_input":
                value = arguments.get(field)
                if isinstance(value, str) and value.strip():
                    instance_values.add(value.strip())
    for value in sorted(instance_values, key=len, reverse=True):
        text = text.replace(value, " ")
    intent = re.sub(r"\s+", " ", text).strip()
    language = "zh" if _CJK.search(intent) else "en"
    return {"text": intent, "language": language}


class DistillationError(ValueError):
    pass


def distillation_eligibility(
    conversation_run: ConversationRun,
    *,
    has_existing_draft: bool = False,
) -> dict[str, Any]:
    """Single server-owned qualification rule for UI and API enforcement."""

    actions = [
        item
        for item in (conversation_run.trajectory_json or {}).get("actions", [])
        if isinstance(item, dict) and item.get("status", "succeeded") == "succeeded"
    ]
    if has_existing_draft:
        reason = "DRAFT_ALREADY_EXISTS"
    elif conversation_run.status != "succeeded":
        reason = "RUN_NOT_SUCCEEDED"
    elif conversation_run.run_type not in {"agent_run", "dsh_session"}:
        reason = "UNSUPPORTED_RUN_TYPE"
    elif not actions:
        reason = "NO_REUSABLE_ACTIONS"
    else:
        reason = "ELIGIBLE"
    return {
        "eligible": reason == "ELIGIBLE",
        "actionCount": len(actions),
        "reasonCode": reason,
    }


class CapabilityDescriptor(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=240)
    use_when: list[str] = Field(alias="useWhen", min_length=1, max_length=8)
    do_not_use_when: list[str] = Field(
        alias="doNotUseWhen", min_length=1, max_length=8
    )
    tags: list[str] = Field(min_length=1, max_length=12)
    examples: list[str] = Field(min_length=1, max_length=6)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("use_when", "do_not_use_when", "tags", "examples")
    @classmethod
    def validate_items(cls, values: list[str]) -> list[str]:
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("CapabilityDescriptor 数组元素必须是非空字符串")
        if any(len(value) > 300 for value in values):
            raise ValueError("CapabilityDescriptor 数组元素过长")
        return values

    @field_validator("name", "summary", "use_when", "do_not_use_when", "tags", "examples")
    @classmethod
    def reject_instance_data(cls, value: Any) -> Any:
        encoded = str(value)
        if re.search(
            r"https?://|\[\[.*?\]\]|(?:api[_-]?key|token|password|secret)\s*[:=]",
            encoded,
            re.I,
        ):
            raise ValueError("CapabilityDescriptor 禁止包含 URL、Secret 或测试指令")
        return value


class PresentationPolicyProposal(BaseModel):
    """Restricted Agent proposal. It is evidence, never the published policy."""

    result_audience: Literal["end_user", "internal", "unknown"] = Field(
        alias="resultAudience"
    )
    consumer: Literal["human", "machine", "composition", "unknown"]
    terminal_or_fragment: Literal["terminal", "fragment", "both", "unknown"] = Field(
        alias="terminalOrFragment"
    )
    enabled: bool
    producer: Literal["template", "llm"]
    template: str = Field(default="执行结果：{json}", max_length=8000)
    prompt: str = Field(
        default="请把以下规范 JSON 整理为简洁、准确的中文说明，不得增加 JSON 中没有的事实：",
        max_length=8000,
    )
    language: str = Field(default="zh-CN", min_length=1, max_length=20)
    max_length: int = Field(default=2000, alias="maxLength", ge=64, le=8000)
    reason: str = Field(min_length=1, max_length=1000)
    evidence: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(ge=0, le=1)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @field_validator("reason", "evidence", "template", "prompt")
    @classmethod
    def reject_sensitive_data(cls, value: Any) -> Any:
        if re.search(
            r"https?://|(?:api[_-]?key|token|password|secret)\s*[:=]",
            str(value),
            re.I,
        ):
            raise ValueError("Presentation proposal 禁止包含 URL 或 Secret")
        return value


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
    deinstantiated_intent = _deinstantiate_intent(trajectory)["text"]

    try:
        actions = build_action_ir(trajectory)
    except TrajectoryAnalysisError as exc:
        raise DistillationError(str(exc)) from exc

    # 失败尝试保留为审计诊断：只读失败仅提示，副作用/终态不确定则阻断发布。
    diagnostics: list[dict[str, str]] = attempt_diagnostics(trajectory)

    proposed_assignments: dict[str, tuple[str, str]] = {}
    if proposal is not None:
        proposed_assignments = _validate_proposal(proposal, actions)

    registry = ActionCompilerRegistry()
    nodes: list[dict[str, Any]] = [{"id": "input", "type": "input", "displayName": "输入"}]
    edges: list[dict[str, str]] = []
    taken = {"input", "output"}
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

    output_provenance = trajectory.get("outputProvenance")
    generative_sources = sorted(
        {
            str(call_id)
            for source in (
                output_provenance.values()
                if isinstance(output_provenance, dict)
                else []
            )
            if isinstance(source, dict) and source.get("mode") == "generative"
            for call_id in source.get("sourceCallIds", [])
        }
    )
    generative_node_id: str | None = None
    if generative_sources:
        unknown_sources = [
            call_id for call_id in generative_sources if call_id not in action_node_ids
        ]
        if unknown_sources:
            diagnostics.append(
                {
                    "level": "error",
                    "code": "INVALID_OUTPUT_PROVENANCE",
                    "path": "output",
                    "message": f"生成式输出引用未知 action: {unknown_sources}",
                }
            )
        else:
            generative_node_id = _node_id("final-response", taken)
            evidence_nodes = list(
                dict.fromkeys(action_node_ids[item] for item in generative_sources)
            )
            nodes.append(
                {
                    "id": generative_node_id,
                    "type": "llm",
                    "displayName": "基于已验证证据生成最终结果",
                    "input": {
                        f"evidence{index + 1}": f"${{{node_id}.output}}"
                        for index, node_id in enumerate(evidence_nodes)
                    },
                    "config": {
                        "outputSchema": output_schema,
                        "maxTotalTokens": 20000,
                        "prompt": (
                            f"任务目标（已去实例化）：{deinstantiated_intent or '完成原任务'}。"
                            "输出字段必须表达该任务的最终结果，并符合 outputSchema 的字段语义；"
                            "目标中的工具查询与自动化操作已由上游节点完成。当前节点只负责汇总结果，"
                            "无需也不得重新执行这些操作；"
                            "仅根据 resolved inputs 中的已验证证据回答，"
                            "不得复述 Schema，"
                            "不得增加证据中不存在的事实。"
                        ),
                    },
                }
            )
            for source_node in evidence_nodes:
                edges.append({"source": source_node, "target": generative_node_id})
            agent_nodes += 1
            last_node = generative_node_id
            last_output_ref = f"${{{generative_node_id}.output}}"

    # 找出所有没有下游的“叶子”节点（终端），连到 output。
    sources_with_downstream = {edge["source"] for edge in edges}
    leaf_nodes = [
        node["id"]
        for node in nodes
        if node["id"] not in sources_with_downstream and node["id"] != "output"
    ]
    output_mapping, output_diagnostics = _build_output_mapping(
        output_schema,
        output_provenance,
        action_node_ids,
        last_node,
        nodes,
        last_output_ref,
        generative_node_id,
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


def _propose_presentation_policy(
    definition: dict[str, Any],
    *,
    agent_proposal: PresentationPolicyProposal | None = None,
) -> dict[str, Any]:
    """Return an Agent proposal or a conservative, explicitly undecided fallback.

    Node types and schema shape cannot reveal who consumes a result. When the
    restricted Agent is unavailable, static code does not guess: Presentation
    stays disabled until a human makes the decision.
    """

    if agent_proposal is None:
        return {
            "resultAudience": "unknown",
            "consumer": "unknown",
            "terminalOrFragment": "unknown",
            "enabled": False,
            "producer": "template",
            "template": "执行结果：{json}",
            "prompt": "请把以下规范 JSON 整理为简洁、准确的中文说明，不得增加 JSON 中没有的事实：",
            "language": "zh-CN",
            "maxLength": 2000,
            "reason": "受限固化 Agent 不可用，静态代码无法从节点类型推断结果受众",
            "evidence": ["仅有 I/O Schema 与数据流，缺少可信受众判断"],
            "confidence": 0.0,
            "source": "platform_fallback",
            "needsHumanDecision": True,
        }
    proposal = agent_proposal.model_dump(by_alias=True)
    # Static enforcement is limited to schema/safety/cost validity.
    if proposal["producer"] == "template" and proposal["enabled"]:
        from app.application.product_history import validate_presentation_template

        validate_presentation_template(
            str(proposal["template"]), definition["spec"]["outputSchema"]
        )
    return {
        **proposal,
        "source": "distillation_agent",
        "needsHumanDecision": proposal["confidence"] < 0.7,
    }


def _enrich_distilled_definition(
    definition: dict[str, Any],
    trajectory: dict[str, Any],
    *,
    capability: CapabilityDescriptor,
    conversation_run_id: str,
    distillation_id: str,
    source_trajectory_digest: str,
    proposal_digest: str | None,
    presentation_proposal: PresentationPolicyProposal | None,
) -> dict[str, Any]:
    """派生可发现、可拆取、可恢复且带完整来源的固化 metadata。"""

    actions = list(trajectory.get("actions") or [])
    action_kinds = sorted(
        {
            str(action.get("kind") or "agent")
            for action in actions
            if isinstance(action, dict)
        }
    )
    logical_id = digest_json(
        {
            "inputSchema": definition["spec"]["inputSchema"],
            "outputSchema": definition["spec"]["outputSchema"],
            "kinds": action_kinds,
            "summary": capability.summary,
        }
    )
    display_name = capability.name
    intent = _deinstantiate_intent(trajectory)
    definition["metadata"] = {
        **definition.get("metadata", {}),
        "name": display_name,
        "logicalWorkflowId": logical_id,
        "capability": capability.model_dump(by_alias=True),
        # 去实例化的源任务意图与语言：作为 manifest 的稳定可检索语料，让源任务的同意图、
        # 同语言查询可以召回自己，而不依赖 Descriptor 生成的语言恰好一致。不含 URL/key/Secret。
        "deInstantiatedIntent": intent,
        "lineage": {
            "sourceConversationRunId": conversation_run_id,
            "sourceTrajectoryDigest": source_trajectory_digest,
            "distillationId": distillation_id,
            "proposalDigest": proposal_digest,
        },
    }

    nodes = definition["spec"]["nodes"]
    recovery_id = "recovery-agent"
    _ensure_recovery_node(definition)

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
    if "callableFragments" not in definition["metadata"]:
        definition["metadata"]["callableFragments"] = _callable_fragment_proposals(
            definition, capability
        )
    proposal = _propose_presentation_policy(
        definition, agent_proposal=presentation_proposal
    )
    # Proposal and reviewer decision are distinct. No executable policy exists
    # until the editor records accept / modified / rejected.
    definition["metadata"]["presentationPolicyProposal"] = proposal
    definition["metadata"]["presentationPolicyDecision"] = {"status": "pending"}
    return definition


def _callable_fragment_proposals(
    definition: dict[str, Any], capability: CapabilityDescriptor
) -> list[dict[str, Any]]:
    business_nodes = [
        node
        for node in definition["spec"]["nodes"]
        if node["type"] not in {"input", "output"}
    ]
    if not business_nodes or any(
        node.get("sideEffect", False) for node in business_nodes
    ):
        return []
    return [
        {
            "name": "verified-task",
            "summary": capability.summary,
            "useWhen": capability.use_when,
            "nodeIds": [node["id"] for node in business_nodes],
        }
    ]


def _ensure_recovery_node(definition: dict[str, Any]) -> None:
    nodes = definition["spec"]["nodes"]
    recoverable = [
        node
        for node in nodes
        if node["type"] in {"browser", "mcp", "skillx"}
        and not node.get("sideEffect", False)
    ]
    recovery_id = "recovery-agent"
    if not recoverable or any(node["id"] == recovery_id for node in nodes):
        return
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
    generative_node_id: str | None = None,
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
        if source.get("mode") == "generative":
            if generative_node_id is None:
                diagnostics.append(
                    {
                        "level": "error",
                        "code": "INVALID_OUTPUT_PROVENANCE",
                        "path": f"output.{key}",
                        "message": f"输出字段 {key} 缺少生成节点",
                    }
                )
            else:
                mapping[key] = f"${{{generative_node_id}.output.{key}}}"
            continue
        action_id = (
            source.get("callId")
            or source.get("actionId")
            or source.get("lastActionId")
        )
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
        path = str(source.get("path", key)).removeprefix("output.")
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


def _default_capability_descriptor(
    trajectory: dict[str, Any],
) -> CapabilityDescriptor:
    # Runtime 不可用时只使用源任务语言和去实例化意图生成通用 fallback；不按
    # browser/file/lookup 等领域或样本分支，稳定 capability 名仅保留为 tag。
    intent = _deinstantiate_intent(trajectory)
    task = intent["text"][:160] or (
        "reuse the verified task with a new input"
        if intent["language"] == "en"
        else "使用新输入复用已验证任务"
    )
    if intent["language"] == "en":
        return CapabilityDescriptor(
            name="Verified task reuse",
            summary=f"Reuse the verified input/output contract for: {task}"[:240],
            useWhen=[f"The new task has the same intent and input category as: {task}"[:300]],
            doNotUseWhen=["The task expands capabilities, secrets, or side effects"],
            tags=["verified-task", "distilled"],
            examples=[f"Run the same task with another compatible input: {task}"[:300]],
        )
    return CapabilityDescriptor(
        name="已验证任务复用",
        summary=f"按冻结输入输出契约复用任务：{task}"[:240],
        useWhen=[f"新任务与以下意图及输入类别一致：{task}"[:300]],
        doNotUseWhen=["任务扩大能力、Secret 或副作用边界"],
        tags=["verified-task", "distilled"],
        examples=[f"使用另一个兼容输入执行同类任务：{task}"[:300]],
    )


def _trajectory_is_analyzable(trajectory: dict[str, Any]) -> bool:
    """轨迹是否可分析为合法 IR（有成功 action、能构建 DAG）。

    仅当轨迹本身可分析时，Proposal/Compiler 的拒绝才持久化为 blocked 供审阅；
    无成功 action、无法构建 IR 等请求级问题仍应直接 4xx。
    """

    try:
        actions = build_action_ir(trajectory)
    except TrajectoryAnalysisError:
        return False
    return bool(actions)


async def _persist_blocked_distillation(
    session: AsyncSession,
    conversation_run: ConversationRun,
    distillation_id: str,
    run_type: str,
    proposal: DistillationProposal | None,
    proposal_source: str,
    *,
    reason: str,
) -> Distillation:
    """持久化一个 blocked Distillation（无草稿 Workflow）+ 结构化诊断。

    修复源 Run/编译规则后可重新发起固化；blocked 记录只用于让 UI 刷新后仍显示阻断原因，
    不占用 ``DRAFT_ALREADY_EXISTS``（该判定只统计已有草稿的记录，见 distill 路由）。
    """

    analysis = {
        "publishable": False,
        "diagnostics": [
            {
                "level": "error",
                "code": "DISTILLATION_REJECTED",
                "path": "proposal",
                "message": reason,
            },
            *attempt_diagnostics(conversation_run.trajectory_json),
        ],
        "sourceRunId": conversation_run.id,
        "sourceRunType": run_type,
        "usedProposal": proposal is not None,
        "proposalSource": proposal_source,
    }
    result = Distillation(
        id=distillation_id,
        conversation_run_id=conversation_run.id,
        status="blocked",
        draft_workflow_id=None,
        analysis_json=analysis,
    )
    session.add(result)
    await session.flush()
    return result


async def distill_conversation_run(
    session: AsyncSession,
    conversation_run: ConversationRun,
) -> Distillation:
    if conversation_run.status != "succeeded":
        raise DistillationError("只有成功的运行可以固化")
    # 只接受服务端 AgentRunService 与 DSH 会话桥接生成的已完成轨迹。外部导入轨迹与
    # 单次 llm_response 都不作为固化来源。
    run_type = getattr(conversation_run, "run_type", "agent_run")
    if run_type not in {"agent_run", "dsh_session"}:
        raise DistillationError(
            "只有服务端 Agent 运行或 DSH 会话桥接运行可固化"
        )
    # 若轨迹携带 TraeX 的 DistillationProposal，则校验其覆盖并让 proposed_strategy 参与裁决。
    proposal: DistillationProposal | None = None
    raw_proposal = conversation_run.trajectory_json.get("proposal")
    proposal_source = "runtime" if isinstance(raw_proposal, dict) else "none"
    # DSH 会话桥接仅提交脱敏后的工具序列，固化过程保持本地确定性：模型拿不到任务
    # 正文，生成草稿也不产生隐式模型请求。
    distillation_runtime_available = (
        run_type == "agent_run" and agent_runtime_config().configured
    )
    distillation_id = str(uuid4())
    if isinstance(raw_proposal, dict):
        try:
            proposal = DistillationProposal.model_validate(raw_proposal)
        except Exception as exc:  # noqa: BLE001 - 可分析轨迹的非法提议必须持久化。
            if not _trajectory_is_analyzable(conversation_run.trajectory_json):
                raise DistillationError(f"非法 DistillationProposal: {exc}") from exc
            return await _persist_blocked_distillation(
                session,
                conversation_run,
                distillation_id,
                run_type,
                None,
                "runtime_invalid",
                reason=f"非法 DistillationProposal: {exc}",
            )
    elif distillation_runtime_available:
        proposal = await _request_distillation_proposal(
            conversation_run.id, conversation_run.trajectory_json
        )
        proposal_source = "distillation_agent" if proposal is not None else "none"
    try:
        definition, analysis = compile_agent_trajectory(
            conversation_run.trajectory_json, proposal
        )
    except DistillationError as exc:
        # 可分析轨迹但 Proposal/Compiler 拒绝：持久化 blocked + 结构化诊断，供刷新后审阅，
        # 而不是回滚后只返回一次性 422。无法分析（无成功 action 等请求级问题）仍向上抛。
        if not _trajectory_is_analyzable(conversation_run.trajectory_json):
            raise
        return await _persist_blocked_distillation(
            session,
            conversation_run,
            distillation_id,
            run_type,
            proposal,
            proposal_source,
            reason=str(exc),
        )
    source_trajectory_digest = digest_json(conversation_run.trajectory_json)
    proposal_digest = (
        digest_json(proposal.model_dump(by_alias=True)) if proposal else None
    )
    capability = _default_capability_descriptor(conversation_run.trajectory_json)
    capability_source = "platform_fallback"
    if distillation_runtime_available:
        proposed_capability = await _request_capability_descriptor(
            conversation_run.id,
            conversation_run.trajectory_json,
        )
        if proposed_capability is not None:
            capability = proposed_capability
            capability_source = "distillation_agent"
    _ensure_recovery_node(definition)
    definition.setdefault("metadata", {})["callableFragments"] = (
        _callable_fragment_proposals(definition, capability)
    )
    presentation_proposal: PresentationPolicyProposal | None = None
    if distillation_runtime_available:
        presentation_proposal = await _request_presentation_proposal(
            conversation_run.id,
            conversation_run.trajectory_json,
            capability,
            definition,
        )
    definition = _enrich_distilled_definition(
        definition,
        conversation_run.trajectory_json,
        capability=capability,
        conversation_run_id=conversation_run.id,
        distillation_id=distillation_id,
        source_trajectory_digest=source_trajectory_digest,
        proposal_digest=proposal_digest,
        presentation_proposal=presentation_proposal,
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
        "capabilityDescriptor": capability.model_dump(by_alias=True),
        "capabilityDescriptorSource": capability_source,
        "logicalWorkflowId": definition["metadata"]["logicalWorkflowId"],
        "fragmentProposals": definition["metadata"].get("callableFragments", []),
        "presentationPolicyProposal": definition["metadata"].get(
            "presentationPolicyProposal", {}
        ),
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
        status="draft_created" if analysis["publishable"] else "blocked",
        draft_workflow_id=workflow.id,
        analysis_json=analysis,
    )
    session.add(result)
    await session.flush()
    return result


async def _request_capability_descriptor(
    run_id: str,
    trajectory: dict[str, Any],
) -> CapabilityDescriptor | None:
    """Ask the restricted distillation stage for instance-independent semantics."""

    action_summary = [
        {
            "kind": action.get("kind"),
            "capability": action.get("capability"),
            "inputFields": sorted((action.get("arguments") or {}).keys()),
            "outputFields": sorted((action.get("result") or {}).keys()),
        }
        for action in trajectory.get("actions", [])
        if isinstance(action, dict)
    ]
    intent = _deinstantiate_intent(trajectory)
    settings = get_settings()
    request = AgentHostRequest(
        run_id=f"capability-{run_id}",
        prompt=(
            "你是受限 CapabilityDescriptor Agent。根据去实例化任务意图、动作类别、能力名"
            "和 I/O 字段描述可复用任务意图与输入类别；禁止复制 URL、Secret、测试指令或"
            "具体输入值。**必须使用与去实例化任务意图相同的自然语言撰写 name/summary/"
            "useWhen/doNotUseWhen/examples**，tags 可同时保留稳定的通用能力名。\n"
            f"deInstantiatedIntent={intent['text']!r} language={intent['language']}\n"
            f"actions={action_summary}"
        ),
        output_schema=CapabilityDescriptor.model_json_schema(by_alias=True),
        working_directory=get_settings().artifact_root / "distillation" / run_id,
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=settings.agent_max_total_tokens,
        max_input_tokens=settings.agent_max_input_tokens,
        max_output_tokens=settings.agent_max_output_tokens,
        max_billable_tokens=settings.agent_max_billable_tokens,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        return CapabilityDescriptor.model_validate(result.output)
    except (AgentHostError, ValueError):
        return None


async def _request_distillation_proposal(
    run_id: str, trajectory: dict[str, Any]
) -> DistillationProposal | None:
    """零工具受限阶段：只对动作摘要提出分组，不执行原任务。"""

    settings = get_settings()
    # 只把成功终态动作作为可执行 action 发给受限 Agent，并显式携带 status；
    # 失败尝试是审计事实，不参与节点分组与覆盖。
    action_summary = [
        {
            "actionId": action.get("callId"),
            "kind": action.get("kind"),
            "capability": action.get("capability") or action.get("tool"),
            "status": action.get("status", "succeeded"),
            "bindings": action.get("argumentBindings", {}),
            "outputSchema": action.get("outputSchema"),
        }
        for action in trajectory.get("actions", [])
        if str(action.get("status", "succeeded")) == "succeeded"
    ]
    request = AgentHostRequest(
        run_id=f"distillation-{run_id}",
        prompt=(
            "你是受限 Distillation Agent。只根据成功动作摘要提出节点分组、策略和变量候选；"
            "每个 action 必须恰好归属一个节点，不得覆盖未提供的动作，不得调用工具。\n"
            f"actions={action_summary}"
        ),
        output_schema=DistillationProposal.model_json_schema(by_alias=True),
        working_directory=settings.artifact_root / "distillation" / run_id,
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=settings.agent_max_total_tokens,
        max_input_tokens=settings.agent_max_input_tokens,
        max_output_tokens=settings.agent_max_output_tokens,
        max_billable_tokens=settings.agent_max_billable_tokens,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        return DistillationProposal.model_validate(result.output)
    except (AgentHostError, ValueError):
        # Runtime 不支持该 Schema 时不伪造 Agent 提议；Compiler 仍可生成带阻断诊断的草稿。
        return None


async def _request_presentation_proposal(
    run_id: str,
    trajectory: dict[str, Any],
    capability: CapabilityDescriptor,
    definition: dict[str, Any],
) -> PresentationPolicyProposal | None:
    """Ask about audience using de-instantiated intent and structural facts only."""

    action_kinds = sorted(
        {
            str(action.get("kind") or "agent")
            for action in trajectory.get("actions", [])
            if isinstance(action, dict)
        }
    )
    trajectory_semantics = trajectory.get("semanticContext")
    trajectory_semantics = (
        trajectory_semantics if isinstance(trajectory_semantics, dict) else {}
    )
    semantic_context = {
        "taskIntent": capability.summary,
        "useWhen": capability.use_when,
        "consumer": trajectory_semantics.get("resultConsumer", "unknown"),
        "entrypoint": trajectory_semantics.get("entrypointKind", "unknown"),
        "topLevelReply": trajectory_semantics.get("topLevelReply", "unknown"),
        "intendedAsFragment": trajectory_semantics.get(
            "intendedAsFragment", "unknown"
        ),
        "inputSchema": definition["spec"]["inputSchema"],
        "outputSchema": definition["spec"]["outputSchema"],
        "dataFlow": {
            "actionKinds": action_kinds,
            "hasCallableFragments": bool(
                definition.get("metadata", {}).get("callableFragments")
            ),
        },
    }
    settings = get_settings()
    request = AgentHostRequest(
        run_id=f"presentation-{run_id}",
        prompt=(
            "你是受限 Presentation 决策 Agent。只根据去实例化任务意图、消费者、顶层/"
            "Fragment 位置、I/O Schema 与数据流提出阅读结果策略。不得复制实例值、Secret、"
            "页面正文或 URL；证据不足时 resultAudience/consumer 使用 unknown、关闭 enabled，"
            "并降低 confidence。template 只能引用 outputSchema 顶层字段或 {json}。\n"
            f"context={semantic_context}"
        ),
        output_schema=PresentationPolicyProposal.model_json_schema(by_alias=True),
        working_directory=get_settings().artifact_root / "distillation" / run_id,
        requested_capabilities=(),
        max_steps=1,
        max_total_tokens=settings.agent_max_total_tokens,
        max_input_tokens=settings.agent_max_input_tokens,
        max_output_tokens=settings.agent_max_output_tokens,
        max_billable_tokens=settings.agent_max_billable_tokens,
        max_duration_seconds=60,
    )
    try:
        result = await AgentHost().execute(request)
        return PresentationPolicyProposal.model_validate(result.output)
    except (AgentHostError, ValueError):
        return None
