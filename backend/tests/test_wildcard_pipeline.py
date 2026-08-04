"""005-F：两类通配任务共用同一固化/编译管线（网页 vs 非网页）。

关键不变量：网页只是 Action Adapter 的一种；NormalizedAction、ActionCompilerRegistry、
compile_agent_trajectory、Checkpoint、Recovery、WorkflowPatch 对两类任务完全一致。
不得存在“仅为 Browser 分叉”的复用框架。
"""

from __future__ import annotations

from app.application.action_compilers import ActionCompilerRegistry, CompileContext
from app.application.distillation import compile_agent_trajectory
from app.application.distillation_ir import build_action_ir

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


def _trajectory(actions: list[dict], *, message: str = "https://site.local/a") -> dict:
    return {
        "runType": "agent_run",
        "name": "wildcard",
        "prompt": message,
        "inputSchema": INPUT_SCHEMA,
        "outputSchema": OUTPUT_SCHEMA,
        "actions": actions,
        "output": {"summary": "首次总结"},
        "config": {"maxSteps": 6, "maxTotalTokens": 30000, "allowedCapabilities": []},
        "originalModelCalls": 2,
    }


def _web_fetch_action(message: str) -> dict:
    # 网页读取：browser.action，携带抽象 Page/Action Contract 证据 → 可确定化为 browser 节点。
    return {
        "callId": "call-1",
        "kind": "browser",
        "capability": "browser.read",
        "arguments": {"url": message},
        "argumentBindings": {"url": {"source": "user_input", "inputKey": "message"}},
        "result": {"title": "T", "content": "正文"},
        "effect": "none",
        "idempotent": True,
        "observations": {
            "action": "read",
            "runtime": "contract",
            "pageContract": {
                "contractVersion": "1",
                "urlPattern": "https://site.local/*",
            },
            "extract": {"content": "#main"},
        },
    }


def _agent_summary_action() -> dict:
    # 显式 Agent 总结：无确定化证据 → 保留为 Agent 节点。
    # 显式声明 text 参数来源于前序读取动作的 content 输出（真实数据依赖）。
    return {
        "callId": "call-2",
        "kind": "agent",
        "capability": "summarize",
        "arguments": {"text": "正文"},
        "argumentBindings": {
            "text": {"source": "prior_output", "fromAction": "call-1", "fromPath": "content"}
        },
        "result": {"summary": "首次总结"},
    }


def _mcp_read_action(message: str) -> dict:
    # 非网页：锁定 MCP 能力（含 registryId/version/digest）→ 确定化为 mcp 节点。
    return {
        "callId": "call-1",
        "kind": "mcp",
        "capability": "file.read",
        "capabilityRef": {"registryId": "snap-1", "version": "1", "digest": "sha256:z"},
        "arguments": {"path": message},
        "result": {"rows": 3},
        "effect": "none",
        "idempotent": True,
    }


def test_web_summary_trajectory_distills_to_mixed_graph() -> None:
    trajectory = _trajectory([_web_fetch_action("https://site.local/a"), _agent_summary_action()])
    definition, analysis = compile_agent_trajectory(trajectory)
    node_types = [n["type"] for n in definition["spec"]["nodes"]]
    # 确定网页读取 + 显式 Agent 总结的混合图，顺序保留，动作不丢。
    assert node_types == ["input", "browser", "agent", "output"]
    assert analysis["directNodes"] == 1
    assert analysis["agentNodes"] == 1
    # browser 节点是抽象契约，不暴露 Playwright 私有对象。
    browser_node = definition["spec"]["nodes"][1]
    assert browser_node["config"]["pageContract"]["contractVersion"] == "1"
    assert "playwright" not in str(browser_node["config"]).lower()
    # 真实数据依赖：Agent 总结节点的 text 输入引用 browser 节点的 content 输出，
    # 而不是常量“正文”。这是可执行 mixed dataflow 的核心证据。
    agent_node = definition["spec"]["nodes"][2]
    browser_id = browser_node["id"]
    assert agent_node["input"]["text"] == f"${{{browser_id}.output.content}}"
    # 边由真实数据依赖构建：browser → agent。
    assert {"source": browser_id, "target": agent_node["id"]} in definition["spec"]["edges"]


def test_non_web_file_task_uses_same_pipeline() -> None:
    trajectory = _trajectory(
        [_mcp_read_action("/data/a.csv"), _agent_summary_action()], message="/data/a.csv"
    )
    definition, analysis = compile_agent_trajectory(trajectory)
    node_types = [n["type"] for n in definition["spec"]["nodes"]]
    # 与网页样例完全相同的管线：确定 MCP 节点 + 显式 Agent 节点。
    assert node_types == ["input", "mcp", "agent", "output"]
    assert analysis["directNodes"] == 1
    assert analysis["agentNodes"] == 1
    assert analysis["capabilityLocks"][0]["digest"] == "sha256:z"


def test_web_fetch_node_reparameterizes_input_for_refetch() -> None:
    # 网页读取节点的 url 绑定到 ${input.message}：复用时以新输入重新获取，而非缓存首次结果。
    trajectory = _trajectory([_web_fetch_action("https://site.local/a")])
    definition, _ = compile_agent_trajectory(trajectory)
    browser_node = definition["spec"]["nodes"][1]
    assert browser_node["input"]["url"] == "${input.message}"


def test_same_ir_and_registry_for_both_task_shapes() -> None:
    # 两类任务用同一 build_action_ir 与 ActionCompilerRegistry。
    registry = ActionCompilerRegistry()
    ctx = CompileContext(node_id="n1", message="m", output_schema=OUTPUT_SCHEMA)

    web_ir = build_action_ir(_trajectory([_web_fetch_action("m")], message="m"))
    non_web_ir = build_action_ir(_trajectory([_mcp_read_action("m")], message="m"))
    assert registry.compile(web_ir[0], ctx).decision == "compiled"
    assert registry.compile(non_web_ir[0], ctx).decision == "compiled"


def test_unknown_web_action_without_contract_preserved_as_agent() -> None:
    # 网页动作缺少抽象契约证据：保留为 Agent 节点，不丢弃、不误确定化。
    action = _web_fetch_action("m")
    action["observations"] = {"action": "read"}  # 无 pageContract
    definition, analysis = compile_agent_trajectory(_trajectory([action], message="m"))
    assert definition["spec"]["nodes"][1]["type"] == "agent"
    assert analysis["agentNodes"] == 1
