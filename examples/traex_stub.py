#!/usr/bin/env python3
"""可重复的 TraeX-compatible fixture，用于 Demo/测试的真实 Agent 主路径。

按 TraeXSubprocessAdapter 约定接收 --prompt/--max-steps/--max-tokens/--allowed-tools，
向 stdout 逐行输出 JSON 事件（agent.plan、tool.call.*、agent.step.completed、
agent.completed）。行为完全确定，不依赖真实模型。

TraeXSubprocessAdapter 只透传 PATH，不透传业务环境变量，因此故障注入通过 prompt 内的
指令携带（Host 把 node.config.prompt 拼进 --prompt）：
- [[MODE:ok|no_output|bad_schema|unauthorized_tool|hang]]
- [[REPLY:文本]]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _directive(prompt: str, key: str, default: str) -> str:
    match = re.search(rf"\[\[{key}:(.*?)\]\]", prompt)
    return match.group(1) if match else default


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

    if mode == "hang":
        # 模拟长时间无进展，交给 Host 超时/取消。
        time.sleep(30)
        return 0

    emit({"type": "agent.plan", "payload": {"summary": "解析输入并调用允许的工具", "steps": 2}})

    # 选择要调用的工具：unauthorized_tool 模式下故意越权。
    tool_name = "banned.tool" if mode == "unauthorized_tool" else (allowed[0] if allowed else None)
    if tool_name is not None:
        emit(
            {
                "type": "tool.call.started",
                "payload": {"tool": tool_name, "arguments": {"q": "demo"}},
            }
        )
        emit(
            {
                "type": "tool.call.completed",
                "payload": {"tool": tool_name, "result": {"ok": True}},
            }
        )
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
