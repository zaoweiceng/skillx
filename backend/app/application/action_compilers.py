from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.application.distillation_ir import NormalizedAction

# 005-C：可扩展的 Action Compiler Registry。
#
# 每个 action kind 注册一个编译器，返回三种裁决之一：
# - compiled：可确定化的节点（有能力锁/纯函数证据）；
# - agent：保留为显式受限 Agent 节点（未识别或概率性动作，绝不删除）；
# - rejected：确定化建议不满足契约（无能力锁、错误副作用、参数来源无法证明、Schema
#   不兼容），产生错误级 diagnostic 使草稿不可发布。
#
# Agent 只能建议；这里的编译器是最终裁决者。


Decision = Literal["compiled", "agent", "rejected"]


@dataclass
class CompileResult:
    decision: Decision
    node: dict[str, Any] | None = None
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""


@dataclass
class CompileContext:
    """编译单个 action 所需的上下文。"""

    node_id: str
    message: str  # 去除指令后的用户输入语义
    output_schema: dict[str, Any]
    proposed_strategy: str = "deterministic"
    # actionId → 已编译节点 id 的映射：把 prior_output/artifact 绑定翻译为 ${nodeId.path}。
    action_node_ids: dict[str, str] = field(default_factory=dict)


class ActionCompiler(Protocol):
    kind: str

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult: ...


def _input_mapping_from_bindings(
    action: NormalizedAction, ctx: CompileContext
) -> dict[str, Any]:
    """把 IR 绑定转成节点 input mapping。

    - user_input → ``${input.<inputKey>}``；
    - prior_output → ``${<上游节点 id>.output.<path>}``（数据依赖真实传值）；
    - artifact → ``${<上游节点 id>.output.<path>}``（Artifact 引用路径）；
    - constant → 原值。
    未知 fromAction 回退为常量，避免生成悬空引用。
    """

    mapping: dict[str, Any] = {}
    for binding in action.input_bindings:
        if binding.source == "user_input":
            mapping[binding.field] = f"${{input.{binding.input_key or 'message'}}}"
        elif binding.source in ("prior_output", "artifact") and binding.from_action:
            upstream = ctx.action_node_ids.get(binding.from_action)
            path = binding.from_path or "output"
            if upstream:
                mapping[binding.field] = f"${{{upstream}.output.{path}}}"
            else:
                mapping[binding.field] = binding.constant
        else:
            mapping[binding.field] = binding.constant
    return mapping


def _agent_input(action: NormalizedAction, ctx: CompileContext) -> dict[str, Any]:
    """Agent/LLM 节点的输入映射：优先使用显式 bindings（含 prior_output 数据依赖），
    没有任何绑定时回退到 ``{message: ${input.message}}``，保证向后兼容。"""

    mapping = _input_mapping_from_bindings(action, ctx)
    return mapping or {"message": "${input.message}"}


def _agent_node(action: NormalizedAction, ctx: CompileContext, reason: str) -> CompileResult:
    """保留为显式受限 Agent 节点（不删除未识别动作）。"""

    node = {
        "id": ctx.node_id,
        "type": "agent",
        "displayName": f"受限 Agent（{action.kind}）",
        "input": _agent_input(action, ctx),
        "config": {
            "outputSchema": ctx.output_schema,
            "maxSteps": 4,
            "maxTotalTokens": 20000,
            "allowedCapabilities": (
                [action.capability_ref.name] if action.capability_ref else []
            ),
            "prompt": action.observations.get("prompt", f"复现动作 {action.action_id}"),
        },
    }
    return CompileResult(decision="agent", node=node, reason=reason)


class LockedCapabilityCompiler:
    """MCP/SkillX：只有解析到已发布能力版本 + digest 才可确定化，否则保留 Agent。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        ref = action.capability_ref
        if ref is None or not (ref.registry_id and ref.version and ref.digest):
            # 无能力锁：不能确定化为锁定节点，保留为显式 Agent 节点。
            return _agent_node(
                action, ctx, "缺少已发布能力版本/digest，无法锁定为确定 MCP/SkillX 节点"
            )
        node_type = "mcp" if self.kind == "mcp" else "skillx"
        config: dict[str, Any] = {"capabilityRef": ref.model_dump(by_alias=True)}
        if node_type == "mcp":
            config["mcpToolSnapshotId"] = ref.registry_id
        else:
            config["capabilityVersionId"] = ref.registry_id
            config["interface"] = ref.interface
        node = {
            "id": ctx.node_id,
            "type": node_type,
            "displayName": f"锁定 {ref.name}",
            "input": _input_mapping_from_bindings(action, ctx),
            "config": config,
            "sideEffect": action.effect not in ("none", "unknown"),
            "idempotent": action.idempotent,
        }
        return CompileResult(decision="compiled", node=node, reason="解析到已发布能力锁")


class BrowserActionCompiler:
    """Browser Read/Extract 只是一个 Adapter：固化为抽象 browser 能力 + BrowserReadContract，
    不暴露 Playwright 私有对象。读取/抽取的正文与结构化字段作为节点输出，供后续 TraeX
    总结引用（真实数据依赖，而非常量“正文”）。"""

    kind = "browser"

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        contract = action.observations.get("pageContract")
        if not isinstance(contract, dict):
            # 没有抽象 Page/Action Contract 证据：保留 Agent 节点复现读取/抽取。
            return _agent_node(action, ctx, "缺少抽象 Page/Action Contract，保留 Agent 读取")
        cap_name = action.capability_ref.name if action.capability_ref else ""
        input_mapping = _input_mapping_from_bindings(action, ctx)
        action_kind = action.observations.get("action", "read")
        config: dict[str, Any] = {
            "runtime": action.observations.get("runtime", "contract"),
            "pageContract": contract,
            "action": action_kind,
        }
        # Read/Extract 契约：正文 selector 与结构化字段 selector，输出 Schema、可重试性。
        for key in ("extract", "outputSchema", "navigation", "timeoutMs"):
            if key in action.observations:
                config[key] = action.observations[key]
        node = {
            "id": ctx.node_id,
            "type": "browser",
            "displayName": f"浏览器动作 {cap_name}",
            "input": input_mapping,
            "config": config,
            # read/extract 是只读语义，不是副作用。
            "sideEffect": action.effect not in ("none", "unknown"),
            "idempotent": action.idempotent,
        }
        return CompileResult(decision="compiled", node=node, reason="固化为抽象 browser 能力")


class TransformCompiler:
    """Transform：只有可证明的纯函数/查表才确定化。删除“任意 dict 即 lookup”。"""

    kind = "transform"

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        operation = action.observations.get("operation")
        if operation == "lookup":
            table = action.observations.get("table")
            key_binding = next(
                (
                    b
                    for b in action.input_bindings
                    if b.source in ("user_input", "prior_output")
                ),
                None,
            )
            if not isinstance(table, dict) or key_binding is None:
                return CompileResult(
                    decision="rejected",
                    diagnostics=[
                        {
                            "level": "error",
                            "code": "UNPROVEN_TRANSFORM",
                            "path": ctx.node_id,
                            "message": "lookup 需要显式全表与来自用户输入/前序输出的 key",
                        }
                    ],
                    reason="lookup 缺少可证明的全表/参数来源",
                )
            # key 引用：user_input→${input.<key>}；prior_output→${上游节点.output.<path>}。
            if key_binding.source == "user_input":
                key_ref = f"${{input.{key_binding.input_key or 'message'}}}"
            else:
                upstream = ctx.action_node_ids.get(key_binding.from_action or "")
                path = key_binding.from_path or "output"
                key_ref = f"${{{upstream}.output.{path}}}" if upstream else "${input.message}"
            node = {
                "id": ctx.node_id,
                "type": "transform",
                "displayName": "确定查表",
                "input": {"key": key_ref},
                "config": {
                    "operation": "lookup",
                    "table": table,
                    "keyField": "key",
                    "default": action.observations.get("default", "未知项"),
                },
            }
            return CompileResult(decision="compiled", node=node, reason="可证明查表")
        if operation in ("passthrough", "pick", "rename", "template", "json"):
            node = {
                "id": ctx.node_id,
                "type": "transform",
                "displayName": "确定转换",
                "input": _input_mapping_from_bindings(action, ctx),
                "config": action.observations.get("config", {"operation": operation}),
            }
            return CompileResult(decision="compiled", node=node, reason="纯函数转换")
        return _agent_node(action, ctx, "无法证明为纯函数/查表，保留 Agent")


class ModelCompiler:
    """LLM/Agent：保留最小输入、提示词模板、输出 Schema、Tool 白名单与预算。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        node = {
            "id": ctx.node_id,
            "type": self.kind,
            "displayName": "受限 Agent" if self.kind == "agent" else "LLM",
            "input": _agent_input(action, ctx),
            "config": {
                "outputSchema": ctx.output_schema,
                "maxSteps": action.observations.get("maxSteps", 4),
                "maxTotalTokens": action.observations.get("maxTotalTokens", 20000),
                "allowedCapabilities": action.observations.get("allowedCapabilities", []),
                "prompt": action.observations.get("prompt", f"复现动作 {action.action_id}"),
            },
        }
        return CompileResult(decision="compiled", node=node, reason="保留显式模型节点")


class ActionCompilerRegistry:
    def __init__(self) -> None:
        self._compilers: dict[str, ActionCompiler] = {
            "mcp": LockedCapabilityCompiler("mcp"),
            "skillx": LockedCapabilityCompiler("skillx"),
            "browser": BrowserActionCompiler(),
            "transform": TransformCompiler(),
            "llm": ModelCompiler("llm"),
            "agent": ModelCompiler("agent"),
        }

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        compiler = self._compilers.get(action.kind)
        if compiler is None:
            # 未注册 kind（含 api/file 未注册为受控能力）：绝不丢弃，保留为 Agent 节点。
            return _agent_node(action, ctx, f"未注册的 action kind {action.kind}，保留 Agent")
        return compiler.compile(action, ctx)
