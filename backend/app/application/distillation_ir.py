from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 005-C：与运行框架无关的 NormalizedAction IR。
#
# 首次 Agent 轨迹被归一化为一串带能力引用、输入绑定、输出 Schema、副作用与可重放性的
# 动作记录。Agent 只能**建议**哪些动作确定化（DistillationProposal）；Compiler 依据
# 能力锁、Schema、副作用、幂等和参数来源证据作最终裁决（见 action_compilers.py）。
#
# 关键不变量：
# - 未识别动作不能删除：要么保留为显式 Agent 节点，要么使草稿不可发布；
# - Tool 结果不因“恰好是 dict”就被当作查表；确定化必须有能力引用/纯函数证据。


BindingSource = Literal["constant", "user_input", "prior_output", "artifact", "unknown"]


class InputBinding(BaseModel):
    """单个输入参数的来源与证据（provenance）。"""

    field: str
    source: BindingSource
    # user_input：绑定到 ${input.<inputKey>}；prior_output：绑定到上游 actionId 的字段。
    input_key: str | None = Field(default=None, alias="inputKey")
    from_action: str | None = Field(default=None, alias="fromAction")
    from_path: str | None = Field(default=None, alias="fromPath")
    constant: Any | None = None

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class CapabilityRef(BaseModel):
    """能力注册引用；只保存名称不足以锁定，必须带 registry id/version/digest。"""

    registry_id: str | None = Field(default=None, alias="registryId")
    name: str
    version: str | None = None
    digest: str | None = None
    interface: str | None = None

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


Replayability = Literal["deterministic", "parameterizable", "agent_only", "unsupported"]
ActionKind = Literal[
    "mcp", "skillx", "browser", "api", "file", "transform", "llm", "agent"
]


class NormalizedAction(BaseModel):
    """一次动作的运行框架无关记录。"""

    action_id: str = Field(alias="actionId")
    kind: str = Field()  # 允许未知 kind（保留为 agent/rejected，不丢弃）
    capability_ref: CapabilityRef | None = Field(default=None, alias="capabilityRef")
    input_bindings: list[InputBinding] = Field(default_factory=list, alias="inputBindings")
    output_schema: dict[str, Any] | None = Field(default=None, alias="outputSchema")
    effect: str = "unknown"
    idempotent: bool = False
    replayability: Replayability = "unsupported"
    # 结果摘要/耗时/错误；大内容只存 Artifact 引用。
    observations: dict[str, Any] = Field(default_factory=dict)
    # 依赖的前序 actionId（形成 DAG）。
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ProposedNode(BaseModel):
    """Agent 对某段动作的分区建议：确定化或保留 Agent。"""

    node_id: str = Field(alias="nodeId")
    action_ids: list[str] = Field(alias="actionIds")
    strategy: Literal["deterministic", "agent", "llm"] = "deterministic"
    reason: str = ""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class DistillationProposal(BaseModel):
    """TraeX 输出的、受 Schema 约束的固化建议。

    Agent 只提出节点边界、变量候选与保留 Agent 的理由；Compiler 逐项验证。
    """

    nodes: list[ProposedNode] = Field(default_factory=list)
    variable_candidates: dict[str, str] = Field(
        default_factory=dict, alias="variableCandidates"
    )
    estimated_cost: dict[str, Any] = Field(default_factory=dict, alias="estimatedCost")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class TrajectoryAnalysisError(ValueError):
    """轨迹无法归一化为合法 IR DAG（缺事件/未闭合调用/输出无法关联）。"""


def _classify_bindings(
    arguments: dict[str, Any],
    message: str,
) -> list[InputBinding]:
    """根据参数值与用户输入的关系推断 provenance。

    - 值等于用户输入 → user_input（绑定 ${input.message}）；
    - 其余 → constant（保留原值）。
    prior_output/artifact 由带 provenance 的事件显式声明时才标注（此处保守处理）。
    """

    bindings: list[InputBinding] = []
    for field, value in arguments.items():
        if message and value == message:
            bindings.append(
                InputBinding(field=field, source="user_input", input_key="message")
            )
        else:
            bindings.append(InputBinding(field=field, source="constant", constant=value))
    return bindings


def build_action_ir(trajectory: dict[str, Any]) -> list[NormalizedAction]:
    """把 AgentHost 归一化 action 序列转为 NormalizedAction IR，并校验 DAG 完整性。

    每个 action 至少要能确定 kind 与输出关联；缺失关键信息时抛
    :class:`TrajectoryAnalysisError`，分析失败而不是猜测。
    """

    message = str(trajectory.get("prompt", ""))
    raw_actions = trajectory.get("actions") or []
    ir: list[NormalizedAction] = []
    previous_id: str | None = None
    for index, raw in enumerate(raw_actions):
        call_id = raw.get("callId")
        if not call_id:
            raise TrajectoryAnalysisError(f"动作 {index} 缺少 callId，无法关联")
        kind = raw.get("kind") or "tool"
        capability_name = raw.get("capability") or raw.get("tool")
        if not capability_name:
            raise TrajectoryAnalysisError(f"动作 {call_id} 缺少能力名称")
        arguments = raw.get("arguments", {})
        result = raw.get("result", {})
        # capabilityRef：只有事件显式携带 registryId/version/digest 才可锁定。
        ref_data = raw.get("capabilityRef")
        if isinstance(ref_data, dict):
            merged = {"name": capability_name, **ref_data}
            capability_ref = CapabilityRef.model_validate(merged)
        else:
            capability_ref = CapabilityRef(name=capability_name)
        # 归一化 kind：AgentHost 已把 browser 动作标为 browser、transform 用 actionKind。
        normalized_kind = raw.get("kind") or kind
        # observations 优先使用事件透传的固化证据（如 lookup 的 operation/table）。
        observations = dict(raw.get("observations") or {})
        observations.setdefault(
            "resultKeys", sorted(result) if isinstance(result, dict) else None
        )
        observations.setdefault("resultType", type(result).__name__)
        ir.append(
            NormalizedAction(
                actionId=call_id,
                kind=normalized_kind,
                capabilityRef=capability_ref,
                inputBindings=_classify_bindings(arguments, message),
                outputSchema=raw.get("outputSchema"),
                effect=raw.get("effect", "unknown"),
                idempotent=bool(raw.get("idempotent", False)),
                replayability=raw.get("replayability", "unsupported"),
                observations=observations,
                dependsOn=[previous_id] if previous_id else [],
            )
        )
        previous_id = call_id
    return ir


AnyNormalizedAction = Annotated[NormalizedAction, Field()]
