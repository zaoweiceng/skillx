#!/usr/bin/env python3
"""可重复的 TraeX-compatible fixture，用于 Demo/测试的真实 Agent 主路径。

按 TraeXSubprocessAdapter 约定接收 --prompt/--max-steps/--max-tokens/--allowed-tools，
向 stdout 逐行输出 JSON 事件（agent.plan、tool.call.*、agent.step.completed、
agent.completed）。行为完全确定，不依赖真实模型。

TraeXSubprocessAdapter 只透传 PATH，不透传业务环境变量，因此故障注入通过 prompt 内的
指令携带（Host 把 node.config.prompt 拼进 --prompt）：
- [[MODE:ok|no_output|bad_schema|unauthorized_tool|hang|many_steps|big_tokens|deterministic]]
- [[MODE:secret_result]]（Tool 参数/结果含 secret-like 字段与多字节大内容，用于验证审计脱敏）
- [[REPLY:文本]]
在 deterministic 模式下，去除指令后的用户输入作为查表 key，Tool 返回整张本地表，
Agent 用 key 选出结果。固化后确定节点用 ${input.message} 作为 key 复现选择。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

# deterministic 模式下 Tool 的确定性查表：输出取决于输入，而非固定 reply 指令。
# 用于 004-D 证明“真实轨迹提炼”——不同输入产生不同结果。
LOOKUP_TABLE = {
    "q1": "第一季度：营收增长 12%",
    "q2": "第二季度：营收增长 8%",
    "q3": "第三季度：营收持平",
    "q4": "第四季度：营收增长 15%",
}


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _directive(prompt: str, key: str, default: str) -> str:
    match = re.search(rf"\[\[{key}:(.*?)\]\]", prompt)
    return match.group(1) if match else default


def _clean_message(prompt: str) -> str:
    """去除 [[...]] 指令后的用户语义输入，用作确定性查表的 key。"""

    return re.sub(r"\[\[.*?\]\]", "", prompt).strip()


def _emit_tool_call(
    call_id: str, tool: str, arguments: dict[str, Any], result: dict[str, Any]
) -> None:
    emit(
        {
            "type": "tool.call.started",
            "payload": {"callId": call_id, "tool": tool, "arguments": arguments},
        }
    )
    emit(
        {
            "type": "tool.call.completed",
            "payload": {"callId": call_id, "tool": tool, "result": result},
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default="")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=30000)
    parser.add_argument("--allowed-tools", default="[]")
    args = parser.parse_args()

    mode = _directive(args.prompt, "MODE", "ok")
    reply = _directive(args.prompt, "REPLY", "已完成任务")
    allowed = json.loads(args.allowed_tools) if args.allowed_tools else []

    if "RecoveryDecision" in args.prompt:
        # 恢复 Agent：平台模板 prompt 含 RecoveryDecision。默认提供替代输出以从剩余
        # 子图继续（provide_substitution）。可用 [[RECOVERY:action]] 覆盖。
        action = _directive(args.prompt, "RECOVERY", "provide_substitution")
        emit({"type": "agent.plan", "payload": {"summary": "评估 checkpoint 决定恢复策略"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 8, "outputTokens": 4},
            }
        )
        decision: dict[str, Any] = {"action": action, "reason": "从 checkpoint 恢复"}
        if action == "provide_substitution":
            recovered_value = _directive(args.prompt, "REPLY", "恢复值")
            decision["substitution"] = {"recovered": True, "value": recovered_value}
        emit(
            {
                "type": "agent.completed",
                "payload": {"steps": 1, "totalTokens": 12, "output": decision},
            }
        )
        return 0

    if mode == "hang":
        # 模拟长时间无进展，交给 Host 超时/取消。
        time.sleep(30)
        return 0

    emit({"type": "agent.plan", "payload": {"summary": "解析输入并调用允许的工具", "steps": 2}})

    if mode == "deterministic":
        # 确定性查表：Tool 事件显式携带固化证据（kind=transform、operation=lookup、整表），
        # Compiler 据此裁决为确定节点，而非依赖“结果恰好是 dict”。
        key = _clean_message(args.prompt)
        tool_name = allowed[0] if allowed else "lookup.table"
        started = {
            "callId": "call-1",
            "tool": tool_name,
            "arguments": {"key": key},
            "actionKind": "transform",
        }
        completed = {
            "callId": "call-1",
            "tool": tool_name,
            "result": dict(LOOKUP_TABLE),
            "actionKind": "transform",
            "replayability": "deterministic",
            "effect": "none",
            "idempotent": True,
            "observations": {"operation": "lookup", "table": dict(LOOKUP_TABLE)},
        }
        emit({"type": "tool.call.started", "payload": started})
        emit({"type": "tool.call.completed", "payload": completed})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 10, "outputTokens": 6},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 16,
                    "output": {"reply": LOOKUP_TABLE.get(key, "未知季度")},
                },
            }
        )
        return 0

    if mode == "many_steps":
        # 超出 Host maxSteps：Host 必须独立强制并终止。
        for step in range(1, 50):
            emit(
                {
                    "type": "agent.step.completed",
                    "payload": {"step": step, "inputTokens": 5, "outputTokens": 5},
                }
            )
        emit({"type": "agent.completed", "payload": {"steps": 49, "output": {"reply": reply}}})
        return 0

    if mode == "big_tokens":
        # 单步返回超大 Token，Host 必须按 maxTotalTokens 终止。
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 900000, "outputTokens": 900000},
            }
        )
        emit({"type": "agent.completed", "payload": {"steps": 1, "output": {"reply": reply}}})
        return 0

    # 选择要调用的工具：unauthorized_tool 模式下故意越权。
    tool_name = "banned.tool" if mode == "unauthorized_tool" else (allowed[0] if allowed else None)
    if mode == "secret_result":
        # Tool 参数/结果携带 secret-like 字段与多字节大内容，用于验证审计事件脱敏与限流。
        secret_tool = allowed[0] if allowed else "lookup.table"
        _emit_tool_call(
            "call-1",
            secret_tool,
            {"api_key": "sk-should-not-leak", "q": "查询"},
            {"password": "p@ss-should-not-leak", "content": "机密正文" * 50},
        )
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 5, "outputTokens": 5},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {"steps": 1, "totalTokens": 10, "output": {"reply": reply}},
            }
        )
        return 0
    if tool_name is not None:
        _emit_tool_call("call-1", tool_name, {"q": "demo"}, {"ok": True})
    emit(
        {
            "type": "agent.step.completed",
            "payload": {"step": 1, "inputTokens": 12, "outputTokens": 8},
        }
    )

    if mode == "no_output":
        # 结束但不给结构化输出，Host 必须判失败。
        emit({"type": "agent.completed", "payload": {"steps": 1, "totalTokens": 20}})
        return 0

    output: dict[str, Any]
    if mode == "bad_schema":
        output = {"unexpected": "field"}
    else:
        output = {"reply": reply}

    emit(
        {
            "type": "agent.completed",
            "payload": {"steps": 1, "totalTokens": 20, "output": output},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
