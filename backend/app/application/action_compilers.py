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


class ActionCompiler(Protocol):
    kind: str

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult: ...


def _input_mapping_from_bindings(action: NormalizedAction) -> dict[str, Any]:
    """把 IR 绑定转成节点 input mapping（user_input→${input.message}，constant→原值）。"""

    mapping: dict[str, Any] = {}
    for binding in action.input_bindings:
        if binding.source == "user_input":
            mapping[binding.field] = "${input.message}"
        elif binding.source == "prior_output" and binding.from_action:
            path = binding.from_path or "output"
            mapping[binding.field] = f"${{{binding.from_action}.{path}}}"
        else:
            mapping[binding.field] = binding.constant
    return mapping


def _agent_node(action: NormalizedAction, ctx: CompileContext, reason: str) -> CompileResult:
    """保留为显式受限 Agent 节点（不删除未识别动作）。"""

    node = {
        "id": ctx.node_id,
        "type": "agent",
        "displayName": f"受限 Agent（{action.kind}）",
        "input": {"message": "${input.message}"},
        "config": {
            "outputSchema": ctx.output_schema,
            "maxSteps": 4,
            "maxTotalTokens": 20000,
            "allowedCapabilities": (
                [action.capability_ref.name] if action.capability_ref else []
            ),
            "prompt": f"复现动作 {action.action_id}",
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
            "input": _input_mapping_from_bindings(action),
            "config": config,
            "sideEffect": action.effect not in ("none", "unknown"),
            "idempotent": action.idempotent,
        }
        return CompileResult(decision="compiled", node=node, reason="解析到已发布能力锁")


class BrowserActionCompiler:
    """Browser：固化为抽象 browser 能力 + Page/Action Contract，不暴露 Playwright 私有对象。"""

    kind = "browser"

    def compile(self, action: NormalizedAction, ctx: CompileContext) -> CompileResult:
        contract = action.observations.get("pageContract")
        if not isinstance(contract, dict):
            # 没有抽象 Page/Action Contract 证据：保留 Agent 节点复现读取/抽取。
            return _agent_node(action, ctx, "缺少抽象 Page/Action Contract，保留 Agent 读取")
        cap_name = action.capability_ref.name if action.capability_ref else ""
        node = {
            "id": ctx.node_id,
            "type": "browser",
            "displayName": f"浏览器动作 {cap_name}",
            "input": _input_mapping_from_bindings(action),
            "config": {
                "runtime": action.observations.get("runtime", "contract"),
                "pageContract": contract,
                "action": action.observations.get("action", "read"),
            },
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
                (b for b in action.input_bindings if b.source == "user_input"), None
            )
            if not isinstance(table, dict) or key_binding is None:
                return CompileResult(
                    decision="rejected",
                    diagnostics=[
                        {
                            "level": "error",
                            "code": "UNPROVEN_TRANSFORM",
                            "path": ctx.node_id,
                            "message": "lookup 需要显式全表与来自用户输入的 key",
                        }
                    ],
                    reason="lookup 缺少可证明的全表/参数来源",
                )
            node = {
                "id": ctx.node_id,
                "type": "transform",
                "displayName": "确定查表",
                "input": {"key": "${input.message}"},
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
                "input": _input_mapping_from_bindings(action),
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
            "input": {"message": "${input.message}"},
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
