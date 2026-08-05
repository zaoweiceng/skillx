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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def read_tool_result(call_id: str) -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("Host 未回送 ToolResult")
    event = json.loads(line)
    payload = event.get("payload", {})
    if (
        event.get("type") != "tool.result"
        or payload.get("callId") != call_id
        or payload.get("status") not in {"succeeded", "failed"}
    ):
        raise RuntimeError("Host ToolResult 协议不匹配")
    return payload


def deterministic_output_provenance(
    field: str,
    tool_result: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    return {
        field: {
            "mode": "deterministic",
            "callId": tool_result["callId"],
            "path": path,
            "evidenceDigest": tool_result["evidenceDigest"],
        }
    }


def generative_output_provenance(
    output: dict[str, Any],
    tool_results: list[dict[str, Any]],
    prompt_version: str,
) -> dict[str, Any]:
    source_call_ids = [item["callId"] for item in tool_results]
    evidence_digests = [item["evidenceDigest"] for item in tool_results]
    return {
        field: {
            "mode": "generative",
            "sourceCallIds": source_call_ids,
            "evidenceDigests": evidence_digests,
            "promptVersion": prompt_version,
        }
        for field in output
    }


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

    if "Progressive WorkflowSelection" in args.prompt:
        user_match = re.search(r"user=(.*)", args.prompt)
        user_text = user_match.group(1).strip() if user_match else _clean_message(args.prompt)
        requested_action = _directive(args.prompt, "SELECTION", "auto")
        emit({"type": "agent.plan", "payload": {"summary": "渐进发现 Workflow"}})
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "workflow-search",
                    "capabilityRef": "workflow.search",
                    "arguments": {"query": user_text, "limit": 5},
                },
            }
        )
        search_result = read_tool_result("workflow-search")
        cards = (search_result.get("output") or {}).get("items", [])
        if not cards or requested_action == "continue_agent":
            output = {
                "action": "continue_agent",
                "reason": "目录没有适合当前任务的能力",
                "refs": [],
                "bindings": {},
                "missingInputs": [],
            }
            emit(
                {
                    "type": "agent.completed",
                    "payload": {
                        "steps": 1,
                        "totalTokens": 20,
                        "output": output,
                        "provenance": [
                            {"callId": "workflow-search", "path": "output.items"}
                        ],
                        "outputProvenance": generative_output_provenance(
                            output, [search_result], "workflow-selection-v1"
                        ),
                    },
                }
            )
            return 0
        workflow_ref = cards[0]["workflowRef"]
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "workflow-describe",
                    "capabilityRef": "workflow.describe",
                    "arguments": {"ref": workflow_ref},
                },
            }
        )
        describe_result = read_tool_result("workflow-describe")
        workflow = (describe_result.get("output") or {}).get("workflow", {})
        required = workflow.get("inputSchema", {}).get("required", [])
        url_match = re.search(r"https?://[^\s]+", user_text)
        lookup_match = re.search(r"\bq\d+\b", user_text)
        concrete_input = (
            url_match.group(0)
            if url_match
            else lookup_match.group(0)
            if lookup_match
            else None
        )
        bindings = (
            {required[0]: concrete_input}
            if len(required) == 1 and concrete_input is not None
            else {}
        )
        action = requested_action
        if action == "auto":
            action = "ask_user" if required and not bindings else "run_full"
        output: dict[str, Any]
        provenance = [
            {"callId": "workflow-search", "path": "output.items"},
            {"callId": "workflow-describe", "path": "output.workflow"},
        ]
        provenance_results = [search_result, describe_result]
        if action == "ask_user":
            output = {
                "action": "ask_user",
                "reason": "已选择能力但缺少必填绑定",
                "refs": [workflow_ref],
                "bindings": {},
                "missingInputs": required or ["input"],
                "question": f"请补充输入：{', '.join(required or ['input'])}",
            }
        elif action == "run_fragment":
            fragments = workflow.get("fragments", [])
            if not fragments:
                output = {
                    "action": "continue_agent",
                    "reason": "已选 Workflow 没有披露可调用 Fragment",
                    "refs": [],
                    "bindings": {},
                    "missingInputs": [],
                }
            else:
                fragment_ref = fragments[0]["fragmentRef"]
                emit(
                    {
                        "type": "tool.requested",
                        "payload": {
                            "callId": "workflow-describe-fragment",
                            "capabilityRef": "workflow.describe_fragment",
                            "arguments": {"ref": fragment_ref},
                        },
                    }
                )
                fragment_result = read_tool_result("workflow-describe-fragment")
                fragment = (fragment_result.get("output") or {}).get("fragment", {})
                fragment_required = fragment.get("boundaryInputSchema", {}).get(
                    "required", []
                )
                provenance.append(
                    {
                        "callId": "workflow-describe-fragment",
                        "path": "output.fragment",
                    }
                )
                provenance_results.append(fragment_result)
                output = {
                    "action": "run_fragment",
                    "reason": "任务只需要已披露局部能力",
                    "refs": [fragment_ref],
                    "bindings": {
                        field: concrete_input or "Selection User"
                        for field in fragment_required
                    },
                    "missingInputs": [],
                }
        elif action == "compose":
            input_schema = workflow.get("inputSchema", {"type": "object"})
            output_schema = workflow.get("outputSchema", {"type": "object"})
            plan = {
                "inputSchema": input_schema,
                "outputSchema": output_schema,
                "steps": [
                    {
                        "id": "selected",
                        "kind": "workflow",
                        "ref": workflow_ref,
                        "input": {
                            key: f"${{input.{key}}}"
                            for key in input_schema.get("properties", {})
                        },
                    }
                ],
                "output": {
                    key: f"${{selected.{key}}}"
                    for key in output_schema.get("properties", {})
                },
            }
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "workflow-compile",
                        "capabilityRef": "workflow.compile_plan",
                        "arguments": {"plan": plan},
                    },
                }
            )
            compile_result = read_tool_result("workflow-compile")
            provenance.append(
                {"callId": "workflow-compile", "path": "output.diagnostics"}
            )
            compiled = compile_result.get("output") or {}
            provenance_results.append(compile_result)
            output = (
                {
                    "action": "compose",
                    "reason": "Plan 已由 Compiler 冻结并通过校验",
                    "refs": [workflow_ref],
                    "bindings": {
                        field: concrete_input or "Selection User"
                        for field in required
                    },
                    "missingInputs": [],
                    "plan": plan,
                }
                if compiled.get("compiled")
                else {
                    "action": "continue_agent",
                    "reason": f"Plan 编译失败：{compiled.get('diagnostics', [])}",
                    "refs": [],
                    "bindings": {},
                    "missingInputs": [],
                }
            )
        else:
            output = {
                "action": "run_full",
                "reason": "L1 契约满足任务需要",
                "refs": [workflow_ref],
                "bindings": {
                    field: concrete_input or "Selection User"
                    for field in required
                },
                "missingInputs": [],
            }
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 20, "outputTokens": 10},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 30,
                    "output": output,
                    "provenance": provenance,
                    "outputProvenance": generative_output_provenance(
                        output, provenance_results, "workflow-selection-v1"
                    ),
                },
            }
        )
        return 0

    if "WorkflowSelection" in args.prompt:
        refs = re.findall(r"workflow:[^'\"\s,}\]]+", args.prompt)
        selected = refs[0] if refs else ""
        emit({"type": "agent.plan", "payload": {"summary": "比较 L0 Workflow 卡片"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 15, "outputTokens": 8},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 23,
                    "output": {
                        "action": "run_full" if selected else "continue_agent",
                        "reason": "TraeX 根据用户语义选择最高匹配 L0 卡片",
                        "refs": [selected] if selected else [],
                        "bindings": {},
                        "missingInputs": [],
                    },
                },
            }
        )
        return 0

    if "CapabilityDescriptor Agent" in args.prompt:
        lowered = args.prompt.lower()
        if "browser.read" in lowered or "'kind': 'browser'" in lowered:
            output = {
                "name": "网页内容读取与总结",
                "summary": "访问网页并基于实时正文生成结构化总结",
                "useWhen": ["需要读取网页当前内容并提取或总结关键信息"],
                "doNotUseWhen": ["目标不是网页读取任务或请求包含未授权副作用"],
                "tags": ["网页", "读取", "总结", "distilled"],
                "examples": ["读取另一个同类网页并总结正文"],
            }
        elif "read.local" in lowered or "'kind': 'file'" in lowered:
            output = {
                "name": "本地文件读取与处理",
                "summary": "读取受控本地文件并按冻结契约处理内容",
                "useWhen": ["需要读取同类本地文件并提取或总结内容"],
                "doNotUseWhen": ["文件不在授权目录或任务需要写入文件"],
                "tags": ["文件", "读取", "处理", "distilled"],
                "examples": ["读取另一个同类文件并返回结构化结果"],
            }
        else:
            output = {
                "name": "结构化查询与结果返回",
                "summary": "根据变化的业务键执行已验证查询并返回结构化结果",
                "useWhen": ["需要使用同类业务键查询冻结数据映射"],
                "doNotUseWhen": ["查询语义或输出契约与已发布版本不一致"],
                "tags": ["查询", "结构化", "lookup", "distilled"],
                "examples": ["使用另一个业务键执行同类查询"],
            }
        emit({"type": "agent.plan", "payload": {"summary": "生成稳定能力描述"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 10, "outputTokens": 8},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {"steps": 1, "totalTokens": 18, "output": output},
            }
        )
        return 0

    if "Distillation Agent" in args.prompt:
        action_ids = re.findall(r"'actionId': '([^']+)'", args.prompt)
        kinds = re.findall(r"'kind': '([^']+)'", args.prompt)
        nodes = [
            {
                "nodeId": f"proposed-{index + 1}",
                "actionIds": [action_id],
                "strategy": (
                    "agent" if index < len(kinds) and kinds[index] == "agent"
                    else "llm" if index < len(kinds) and kinds[index] == "llm"
                    else "deterministic"
                ),
                "reason": "按动作能力边界分组",
            }
            for index, action_id in enumerate(action_ids)
        ]
        emit({"type": "agent.plan", "payload": {"summary": "生成固化分组建议"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 12, "outputTokens": 8},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": {
                        "nodes": nodes,
                        "estimatedCost": {"llmCalls": sum(
                            node["strategy"] in {"agent", "llm"} for node in nodes
                        )},
                    },
                },
            }
        )
        return 0

    if "Revision Agent" in args.prompt:
        version_match = re.search(r"baseVersionId=([^\s]+)", args.prompt)
        node_match = re.search(r"failedNodeId=([^\s]+)", args.prompt)
        failed_config: dict[str, Any] = {}
        config_match = re.search(r"failedNodeConfig=(\{.*\})", args.prompt)
        if config_match:
            try:
                failed_config = json.loads(config_match.group(1))
            except json.JSONDecodeError:
                failed_config = {}
        if isinstance(failed_config.get("pageContract"), dict):
            changes = {
                "config": {
                    "pageContract": {
                        "outputSchema": {
                            "type": "object",
                            "required": ["content"],
                            "properties": {"content": {"type": "string"}},
                        }
                    }
                }
            }
        else:
            changes = {"config": {"operation": "passthrough"}}
        emit({"type": "agent.plan", "payload": {"summary": "生成受控修订提议"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 12, "outputTokens": 8},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": {
                        "kind": "WorkflowPatch",
                        "baseVersionId": (
                            version_match.group(1) if version_match else "unknown"
                        ),
                        "nodePatches": [
                            {
                                "nodeId": (
                                    node_match.group(1) if node_match else "unknown"
                                ),
                                "changes": changes,
                            }
                        ],
                        "addEdges": [],
                        "removeEdges": [],
                        "reason": "baseline 可复现且修订受 Schema 限制",
                    },
                },
            }
        )
        return 0

    if "RecoveryDecision" in args.prompt:
        # 恢复 Agent：平台模板 prompt 含 RecoveryDecision。默认提供替代输出以从剩余
        # 子图继续（provide_substitution）。可用 [[RECOVERY:action]] 覆盖。
        context: dict[str, Any] = {}
        if "context: " in args.prompt:
            try:
                context = json.loads(args.prompt.split("context: ", 1)[1])
            except json.JSONDecodeError:
                context = {}
        run_input = context.get("runInput", {})
        seed = run_input.get("seed") if isinstance(run_input, dict) else None
        action = _directive(args.prompt, "RECOVERY", "provide_substitution")
        if seed == "request-user":
            marker = Path(".recovery-user-answered")
            if marker.exists() or context.get("userAnswer") is not None:
                action = "provide_substitution"
            else:
                marker.write_text("asked", encoding="utf-8")
                action = "request_user"
        elif seed == "retry-replanned":
            action = "retry_replanned"
        if "crash-recovery" in args.prompt:
            marker = Path(".recovery-crash-observed")
            if not marker.exists():
                marker.write_text("claimed", encoding="utf-8")
                time.sleep(30)
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
        elif action == "request_user":
            decision["question"] = "请确认是否按 checkpoint 继续恢复？"
        elif action == "retry_replanned":
            decision["planPatch"] = {
                "nodePatches": [
                    {
                        "nodeId": "risky",
                        "changes": {"config": {"operation": "passthrough"}},
                    }
                ],
                "inputMappings": {},
                "edgePatches": [],
            }
        emit(
            {
                "type": "agent.completed",
                "payload": {"steps": 1, "totalTokens": 12, "output": decision},
            }
        )
        return 0

    if "AgentRun Recovery Agent" in args.prompt:
        checkpoint: dict[str, Any] = {}
        original_task = ""
        if "checkpoint=" in args.prompt:
            try:
                checkpoint = json.loads(
                    args.prompt.split("checkpoint=", 1)[1].split(
                        "\noriginalTask=", 1
                    )[0]
                )
                original_task = args.prompt.split(
                    "\noriginalTask=", 1
                )[1]
            except json.JSONDecodeError:
                checkpoint = {}
        completed = checkpoint.get("completed", [])
        latest = completed[-1].get("output", {}) if completed else {}
        provenance_result: dict[str, Any] | None = None
        if "[[MODE:browser_failure_handoff]]" in original_task:
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "recovery-preflight",
                        "capabilityRef": "lookup.table",
                        "action": "lookup",
                        "actionKind": "transform",
                        "arguments": {"key": "q1"},
                        "effect": "none",
                        "idempotent": True,
                    },
                }
            )
            read_tool_result("recovery-preflight")
            url_match = re.search(r"https?://[^\s\"}]+", original_task)
            url = url_match.group(0) if url_match else _clean_message(original_task)
            parsed_url = urlsplit(url)
            page_contract = {
                "contractVersion": "1",
                "urlPattern": f"{parsed_url.scheme}://{parsed_url.netloc}/*",
                "bodySelector": "#main",
                "fields": {},
            }
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "recovery-browser",
                        "capabilityRef": "browser.read",
                        "action": "read",
                        "actionKind": "browser",
                        "arguments": {
                            "url": url,
                            "pageContract": page_contract,
                        },
                        "argumentBindings": {
                            "url": {
                                "source": "user_input",
                                "inputKey": "message",
                            }
                        },
                        "effect": "none",
                        "idempotent": True,
                        "replayability": "parameterizable",
                        "outputSchema": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"}
                            },
                            "required": ["content"],
                        },
                        "observations": {
                            "runtime": "playwright",
                            "action": "read",
                            "pageContract": page_contract,
                        },
                    },
                }
            )
            provenance_result = read_tool_result("recovery-browser")
            latest = provenance_result.get("output", {})
        if "[[RECOVERY_REPEAT]]" in original_task:
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "recovery-repeat",
                        "capabilityRef": "lookup.table",
                        "arguments": {
                            "key": _clean_message(original_task)
                        },
                    },
                }
            )
            provenance_result = read_tool_result("recovery-repeat")
            latest = provenance_result.get("output", {})
        reply_value = latest.get("value", latest.get("content", "恢复完成"))
        emit({"type": "agent.plan", "payload": {"summary": "从 checkpoint 剩余步骤继续"}})
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 8, "outputTokens": 4},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 12,
                    "output": {"reply": reply_value},
                    **(
                        {
                            "outputProvenance": deterministic_output_provenance(
                                "reply",
                                provenance_result,
                                (
                                    "output.content"
                                    if "content"
                                    in provenance_result.get("output", {})
                                    else "output.value"
                                ),
                            )
                        }
                        if provenance_result is not None
                        else {}
                    ),
                },
            }
        )
        return 0

    if mode == "hang":
        # 模拟长时间无进展，交给 Host 超时/取消。
        time.sleep(30)
        return 0

    emit({"type": "agent.plan", "payload": {"summary": "解析输入并调用允许的工具", "steps": 2}})

    if mode in {
        "deterministic",
        "deterministic_bad_output",
        "deterministic_fail_after_tool",
    }:
        # Agent 只提交 key；整表、选值与固化 evidence 均由 Host Adapter 返回。
        key = _clean_message(args.prompt)
        tool_name = allowed[0] if allowed else "lookup.table"
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-1",
                    "capabilityRef": tool_name,
                    "arguments": {"key": key},
                    "argumentBindings": {
                        "key": {"source": "user_input", "inputKey": "message"}
                    },
                    "actionKind": "transform",
                    "replayability": "deterministic",
                    "effect": "none",
                    "idempotent": True,
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "table": {"type": "object"},
                            "value": {"type": "string"},
                        },
                        "required": ["table", "value"],
                    },
                },
            }
        )
        tool_result = read_tool_result("call-1")
        output = tool_result.get("output") or {}
        if mode == "deterministic_fail_after_tool":
            return 0
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
                    "output": {
                        "reply": (
                            "fixture-constant"
                            if mode == "deterministic_bad_output"
                            else output.get("value", "未知季度")
                        )
                    },
                    "provenance": [{"callId": "call-1", "path": "output.value"}],
                    "outputProvenance": deterministic_output_provenance(
                        "reply", tool_result, "output.value"
                    ),
                },
            }
        )
        return 0

    if mode in {
        "browser_summary",
        "browser_failure_handoff",
        "browser_invalid_completed",
    }:
        if mode == "browser_failure_handoff":
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "call-preflight",
                        "capabilityRef": "lookup.table",
                        "action": "lookup",
                        "actionKind": "transform",
                        "arguments": {"key": "q1"},
                        "effect": "none",
                        "idempotent": True,
                    },
                }
            )
            preflight = read_tool_result("call-preflight")
            if preflight["status"] != "succeeded":
                emit(
                    {
                        "type": "agent.failed",
                        "payload": {
                            "code": preflight["error"]["code"],
                            "message": preflight["error"]["message"],
                            "retryable": preflight["error"].get(
                                "retryable", False
                            ),
                            "callId": "call-preflight",
                        },
                    }
                )
                return 0
        url_match = re.search(r"https?://[^\s\"}]+", args.prompt)
        url = url_match.group(0) if url_match else _clean_message(args.prompt)
        parsed_url = urlsplit(url)
        browser_capability = "browser.read"
        summary_capability = "summarize.text"
        page_contract = {
            "contractVersion": "1",
            "urlPattern": f"{parsed_url.scheme}://{parsed_url.netloc}/*",
            "bodySelector": "#main",
            "fields": {},
        }
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-browser",
                    "capabilityRef": browser_capability,
                    "action": "read",
                    "actionKind": "browser",
                    "arguments": {"url": url, "pageContract": page_contract},
                    "argumentBindings": {
                        "url": {"source": "user_input", "inputKey": "message"}
                    },
                    "effect": "none",
                    "idempotent": True,
                    "replayability": "parameterizable",
                    "outputSchema": {
                        "type": "object",
                        "properties": {"content": {"type": "string"}},
                        "required": ["content"],
                    },
                    "observations": {
                        "runtime": "playwright",
                        "action": "read",
                        "pageContract": page_contract,
                    },
                },
            }
        )
        tool_result = read_tool_result("call-browser")
        if tool_result["status"] != "succeeded":
            if mode == "browser_invalid_completed":
                emit(
                    {
                        "type": "agent.completed",
                        "payload": {
                            "steps": 1,
                            "totalTokens": 10,
                            "output": {
                                "reply": tool_result["error"]["message"]
                            },
                        },
                    }
                )
                return 0
            emit(
                {
                    "type": "agent.failed",
                    "payload": {
                        "code": tool_result["error"]["code"],
                        "message": tool_result["error"]["message"],
                        "retryable": tool_result["error"].get(
                            "retryable", False
                        ),
                        "callId": "call-browser",
                    },
                }
            )
            return 0
        browser_output = tool_result["output"]
        browser_content = browser_output["content"]
        emit(
            {
                "type": "tool.call.started",
                "payload": {
                    "callId": "call-summary",
                    "tool": summary_capability,
                    "arguments": {"text": browser_content},
                    "argumentBindings": {
                        "text": {
                            "source": "prior_output",
                            "fromAction": "call-browser",
                            "fromPath": "content",
                        }
                    },
                },
            }
        )
        emit(
            {
                "type": "tool.call.completed",
                "payload": {
                    "callId": "call-summary",
                    "tool": summary_capability,
                    "actionKind": "agent",
                    "result": {"reply": browser_content},
                    "outputSchema": {
                        "type": "object",
                        "properties": {"reply": {"type": "string"}},
                        "required": ["reply"],
                        "additionalProperties": False,
                    },
                    "observations": {
                        "prompt": "[[MODE:echo_input]] 根据 text 生成 reply",
                        "allowedCapabilities": [],
                        "maxSteps": 2,
                    },
                },
            }
        )
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 20, "outputTokens": 10},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 30,
                    "output": {"reply": browser_content},
                    "provenance": [
                        {"callId": "call-browser", "path": "output.content"}
                    ],
                    "outputProvenance": deterministic_output_provenance(
                        "reply", tool_result, "output.content"
                    ),
                },
            }
        )
        return 0

    if mode == "local_summary":
        path = _directive(args.prompt, "FILE", "source.txt")
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-local",
                    "capabilityRef": "read.local",
                    "action": "read",
                    "actionKind": "file",
                    "arguments": {"path": path},
                    "argumentBindings": {
                        "path": {"source": "constant", "value": path}
                    },
                    "effect": "none",
                    "idempotent": True,
                    "replayability": "parameterizable",
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "bytes": {"type": "integer"},
                        },
                        "required": ["path", "content", "bytes"],
                    },
                },
            }
        )
        tool_result = read_tool_result("call-local")
        output = tool_result.get("output") or {}
        reply = output.get("content", tool_result.get("error", {}).get("message", ""))
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 10, "outputTokens": 10},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": {"reply": reply},
                    "provenance": [
                        {
                            "callId": "call-local",
                            "path": (
                                "output.content"
                                if tool_result["status"] == "succeeded"
                                else "error.message"
                            ),
                        }
                    ],
                    **(
                        {
                            "outputProvenance": deterministic_output_provenance(
                                "reply", tool_result, "output.content"
                            )
                        }
                        if tool_result["status"] == "succeeded"
                        else {}
                    ),
                },
            }
        )
        return 0

    if mode == "echo_input":
        marker = "\n输入："
        raw_input = args.prompt.rsplit(marker, 1)[-1] if marker in args.prompt else "{}"
        try:
            input_data = json.loads(raw_input)
        except json.JSONDecodeError:
            input_data = {}
        echoed = input_data.get("text") or input_data.get("content") or str(input_data)
        emit(
            {
                "type": "agent.step.completed",
                "payload": {"step": 1, "inputTokens": 10, "outputTokens": 10},
            }
        )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": {"reply": str(echoed)},
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
