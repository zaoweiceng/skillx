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
import hashlib
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


def _fixture_value(schema: dict[str, Any], field: str, reply: str) -> Any:
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", list(properties))
        return {
            name: _fixture_value(properties.get(name, {}), name, reply)
            for name in required
        }
    if kind == "array":
        return []
    if kind in {"integer", "number"}:
        return 0
    if kind == "boolean":
        return False
    if field in {"reply", "text"}:
        return reply
    return "fixture"


def _plan_step(
    step_id: str,
    capability: str,
    *,
    action: str,
    depends_on: list[str] | None = None,
    argument_refs: dict[str, Any] | None = None,
    output_name: str,
    fallback: str = "abort",
) -> dict[str, Any]:
    return {
        "id": step_id,
        "action": action,
        "capabilityRef": capability,
        "dependsOn": depends_on or [],
        "argumentRefs": argument_refs or {},
        "effect": "none",
        "idempotencyKey": None,
        "outputName": output_name,
        "fallback": fallback,
    }


def _structured_plan(
    plan_id: str,
    summary: str,
    steps: list[dict[str, Any]],
    *,
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "type": "agent.plan",
        "payload": {
            "schemaVersion": "1",
            "planId": plan_id,
            "revision": revision,
            "summary": summary,
            "steps": steps,
            "replanBoundary": "remaining_only",
        },
    }


def _plan_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _planned_request_meta(
    plan: dict[str, Any] | None, step_id: str, *, attempt: int = 1
) -> dict[str, Any]:
    if plan is None:
        return {}
    step = next(
        item for item in plan.get("steps", []) if item.get("id") == step_id
    )
    return {
        "planStepId": step_id,
        "planDigest": _plan_digest(plan),
        "planRevision": plan.get("revision", 1),
        "stepAttempt": attempt,
        "idempotencyKey": step.get("idempotencyKey"),
    }


def _tool_arguments(
    step: dict[str, Any],
    original_input: str,
    outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for name, reference in step.get("argumentRefs", {}).items():
        if not isinstance(reference, dict):
            continue
        source = reference.get("source")
        if source == "literal":
            arguments[name] = reference.get("value")
        elif source == "user_input":
            value = _clean_message(original_input)
            if reference.get("extract") == "url":
                match = re.search(r"https?://[^\s\"}]+", value)
                value = match.group(0) if match else value
            arguments[name] = value
        elif source == "step_output":
            value: Any = outputs.get(str(reference.get("stepId")), {})
            for part in str(reference.get("path", "")).split("."):
                if part and isinstance(value, dict):
                    value = value.get(part)
            arguments[name] = value
    return arguments


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
    parser.add_argument("--output-schema", default="{}")
    args = parser.parse_args()

    terminal_task = re.search(r"(?:^|\n)taskData=(\"(?:\\.|[^\"])*\")", args.prompt)
    if terminal_task:
        args.prompt = json.loads(terminal_task.group(1))

    mode = _directive(args.prompt, "MODE", "ok")
    reply = _directive(args.prompt, "REPLY", "已完成任务")
    allowed = json.loads(args.allowed_tools) if args.allowed_tools else []
    output_schema = json.loads(args.output_schema) if args.output_schema else {}

    if output_schema.get("title") == "TaskDecision":
        context = json.loads(args.prompt.split("\n", 1)[1])
        calls = context["calls"]
        answers = context["humanAnswers"]
        output = {
            "action": "complete",
            "summary": "完成问候",
            "completionCriteria": ["输出问候"],
            "remaining": [],
            "calls": [],
            "questions": [],
            "evidence": [],
            "answer": "",
        }
        if calls:
            output["evidence"] = [x["id"] for x in calls if x["status"] == "succeeded"]
            output["answer"] = json.dumps(calls[-1]["output"], ensure_ascii=False)
        elif "[[HUMAN]]" in context["goal"] and not answers:
            output.update(
                action="human.request",
                questions=[
                    {
                        "key": "name",
                        "kind": "information",
                        "title": "请提供姓名",
                        "reason": "问候需要姓名",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"name": {"type": "string", "title": "姓名"}},
                            "required": ["name"],
                        },
                    }
                ],
            )
        else:
            workflow = next(
                x
                for x in context["catalog"]["capabilities"]
                if any(word in json.dumps(x, ensure_ascii=False) for word in ("greeting", "问候"))
            )
            name = (
                answers[0]["answer"]["name"] if answers
                else context["state"].get("userInputs", {}).get("name", "Alice")
            )
            output.update(
                action="dispatch",
                calls=[
                    {
                        "key": "greet",
                        "kind": "capability",
                        "arguments": {
                            "capabilityVersionId": workflow["id"],
                            "interface": "execute",
                            "input": {"name": name},
                        },
                        "reason": "复用问候能力",
                    }
                ],
            )
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": output,
                    "outputProvenance": generative_output_provenance(
                        output, [], "task-v1"
                    ),
                },
            }
        )
        return 0

    if "WorkflowBindingExtraction" in args.prompt:
        schema_match = re.search(r"^inputSchema=(.*)$", args.prompt, re.MULTILINE)
        required_match = re.search(r"^required=(.*)$", args.prompt, re.MULTILINE)
        packets_match = re.search(r"^messagePackets=(.*)$", args.prompt, re.MULTILINE)
        input_schema = json.loads(schema_match.group(1)) if schema_match else {}
        required = json.loads(required_match.group(1)) if required_match else []
        packets = json.loads(packets_match.group(1)) if packets_match else []
        current = packets[-1] if packets else {"messageId": "unknown", "text": ""}
        current_text = str(current.get("text", ""))
        requested_action = _directive(current_text, "SELECTION", "auto")
        cleaned = _clean_message(current_text)
        explicit = re.search(
            r"(?:参数是|=)\s*([^，。！？；,;\s]+)", cleaned
        )
        force_missing = (
            requested_action == "ask_user"
            or (requested_action == "auto" and explicit is None)
            or _directive(current_text, "BINDING", "found") == "missing"
        )
        force_ambiguous = _directive(current_text, "BINDING", "found") == "ambiguous"
        candidate_value = explicit.group(1) if explicit else cleaned
        evidence_text = explicit.group(0) if explicit else candidate_value
        evidence_start = current_text.find(evidence_text)
        evidence_end = evidence_start + len(evidence_text)
        bindings: dict[str, Any] = {}
        sources: list[dict[str, Any]] = []
        assessments: list[dict[str, str]] = []
        missing: list[str] = []
        for field in required:
            if force_missing:
                assessments.append(
                    {"field": field, "status": "missing", "reason": "消息中没有明确值"}
                )
                missing.append(field)
                continue
            if force_ambiguous:
                assessments.append(
                    {"field": field, "status": "ambiguous", "reason": "消息中有多个候选值"}
                )
                missing.append(field)
                continue
            field_schema = (input_schema.get("properties") or {}).get(field, {})
            value: Any = candidate_value
            if field_schema.get("type") == "integer" and re.fullmatch(r"[-+]?\d+", candidate_value):
                value = int(candidate_value)
            elif field_schema.get("type") == "number":
                try:
                    value = float(candidate_value)
                except ValueError:
                    value = candidate_value
            bindings[field] = value
            sources.append(
                {
                    "field": field,
                    "source": "user_message",
                    "messageId": current.get("messageId", "unknown"),
                    "rawSpan": {
                        "start": evidence_start,
                        "end": evidence_end,
                        "text": evidence_text,
                    },
                    "canonicalValue": value,
                    "confidence": 1,
                    "uncertain": False,
                }
            )
            if _directive(current_text, "SOURCE", "valid") == "forged":
                sources[-1]["rawSpan"] = {
                    "start": 0,
                    "end": 3,
                    "text": "不存在",
                }
            assessments.append(
                {"field": field, "status": "found", "reason": "当前消息包含明确原文"}
            )
        output = {
            "bindings": bindings,
            "bindingSources": sources,
            "missingInputs": missing,
            "fieldAssessments": assessments,
            "question": f"请补充输入：{', '.join(missing)}" if missing else None,
        }
        emit(
            {
                "type": "agent.completed",
                "payload": {
                    "steps": 1,
                    "totalTokens": 20,
                    "output": output,
                    "outputProvenance": generative_output_provenance(
                        output, [], "workflow-binding-v1"
                    ),
                },
            }
        )
        return 0

    if (
        "Progressive WorkflowSelection" in args.prompt
        or "WorkflowSelectionDisclosureContinuation" in args.prompt
    ):
        use_catalog = "workflow.catalog" in allowed

        def catalog_call(
            legacy_capability: str, action: str, arguments: dict[str, Any]
        ) -> tuple[str, dict[str, Any]]:
            if use_catalog:
                return "workflow.catalog", {"action": action, **arguments}
            return legacy_capability, arguments

        user_match = re.search(r"user=(.*)", args.prompt)
        user_text = user_match.group(1).strip() if user_match else _clean_message(args.prompt)
        if "WorkflowSelectionDisclosureContinuation" in args.prompt:
            previous_match = re.search(r"^previousSelection=(.*)$", args.prompt, re.MULTILINE)
            diagnostic_match = re.search(r"^diagnostic=(.*)$", args.prompt, re.MULTILINE)
            previous = json.loads(previous_match.group(1)) if previous_match else {}
            diagnostic = json.loads(diagnostic_match.group(1)) if diagnostic_match else {}
            provenance_results: list[dict[str, Any]] = []
            if _directive(user_text, "DISCLOSURE", "complete") != "never":
                action = diagnostic.get("requiredAction", "describe")
                arguments = {"action": action}
                if diagnostic.get("ref"):
                    arguments["ref"] = diagnostic["ref"]
                if action == "compile_plan":
                    arguments["plan"] = previous.get("plan")
                emit(
                    {
                        "type": "tool.requested",
                        "payload": {
                            "callId": "workflow-disclosure-correction",
                            "capabilityRef": "workflow.catalog",
                            "arguments": arguments,
                        },
                    }
                )
                provenance_results.append(
                    read_tool_result("workflow-disclosure-correction")
                )
            output = {
                key: previous[key]
                for key in ("action", "reason", "refs", "plan", "requiredCapabilities")
                if key in previous and previous[key] is not None
            }
            emit(
                {
                    "type": "agent.completed",
                    "payload": {
                        "steps": 1,
                        "totalTokens": 20,
                        "output": output,
                        "outputProvenance": generative_output_provenance(
                            output, provenance_results, "workflow-disclosure-v1"
                        ),
                    },
                }
            )
            return 0
        requested_action = _directive(args.prompt, "SELECTION", "auto")
        emit({"type": "agent.plan", "payload": {"summary": "渐进发现 Workflow"}})
        search_capability, search_arguments = catalog_call(
            "workflow.search", "search", {"query": user_text, "limit": 5}
        )
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "workflow-search",
                    "capabilityRef": search_capability,
                    "arguments": search_arguments,
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
        describe_capability, describe_arguments = catalog_call(
            "workflow.describe", "describe", {"ref": workflow_ref}
        )
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "workflow-describe",
                    "capabilityRef": describe_capability,
                    "arguments": describe_arguments,
                },
            }
        )
        describe_result = read_tool_result("workflow-describe")
        workflow = (describe_result.get("output") or {}).get("workflow", {})
        input_synopsis = workflow.get("inputSynopsis", {})
        output_synopsis = workflow.get("outputSynopsis", {})
        required = input_synopsis.get("required", [])
        url_match = re.search(r"https?://[^\s]+", user_text)
        lookup_match = re.search(r"\bq\d+\b", user_text)
        concrete_input = (
            url_match.group(0)
            if url_match
            else lookup_match.group(0)
            if lookup_match
            else _clean_message(user_text)
            if requested_action in {"run_full", "run_fragment", "compose"}
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
        elif (
            action == "run_fragment"
            and _directive(user_text, "DISCLOSURE", "complete")
            in {"incomplete", "never"}
        ):
            fragments = workflow.get("fragments", [])
            fragment_ref = fragments[0]["fragmentRef"]
            output = {
                "action": "run_fragment",
                "reason": "先返回仅有 L1 的 Fragment 选择，用于服务端约束测试",
                "refs": [fragment_ref],
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
                fragment_capability, fragment_arguments = catalog_call(
                    "workflow.describe_fragment",
                    "describe_fragment",
                    {"ref": fragment_ref},
                )
                emit(
                    {
                        "type": "tool.requested",
                        "payload": {
                            "callId": "workflow-describe-fragment",
                            "capabilityRef": fragment_capability,
                            "arguments": fragment_arguments,
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
            input_schema = {
                "type": "object",
                "properties": {
                    key: {"type": value}
                    for key, value in input_synopsis.get("properties", {}).items()
                },
                "required": required,
            }
            output_schema = {
                "type": "object",
                "properties": {
                    key: {"type": value}
                    for key, value in output_synopsis.get("properties", {}).items()
                },
            }
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
            compile_capability, compile_arguments = catalog_call(
                "workflow.compile_plan", "compile_plan", {"plan": plan}
            )
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "workflow-compile",
                        "capabilityRef": compile_capability,
                        "arguments": compile_arguments,
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
        if output.get("action") == "ask_user":
            output["action"] = "run_full"
            output["reason"] = "L1 契约满足任务；参数由第二阶段解析"
            output.pop("question", None)
        for key in ("bindings", "bindingSources", "missingInputs", "compiledPlan"):
            output.pop(key, None)
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
        failed_input = context.get("failedNode", {}).get("input", {})
        seed = failed_input.get("value") if isinstance(failed_input, dict) else None
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
        if "crash-recovery" in args.prompt and context.get("recoveryAttempt", 1) <= 1:
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
        continuation: dict[str, Any] = {}
        if "continuationRequest=" in args.prompt:
            try:
                continuation = json.loads(
                    args.prompt.split("continuationRequest=", 1)[1]
                )
            except json.JSONDecodeError:
                continuation = {}
        original_input = str(continuation.get("originalInput", ""))
        completed = continuation.get("completedOutputs", [])
        completed_plan_steps = continuation.get("completedPlanSteps", [])
        remaining_plan = continuation.get("remainingPlan") or {}
        remaining_steps = remaining_plan.get("steps", [])
        source_plan = continuation.get("sourcePlan")
        source_attempts = continuation.get("stepAttempts", {})
        if (
            not isinstance(source_plan, dict)
            and not remaining_steps
            and continuation.get("userAnswer", {}).get("continue") is True
            and "lookup.table" in continuation.get("capabilityLocks", [])
        ):
            legacy_key = _clean_message(original_input) or "q1"
            source_plan = _structured_plan(
                "legacy-replan",
                "人工确认后的受限 legacy replan",
                [
                    _plan_step(
                        "legacy-lookup",
                        "lookup.table",
                        action="lookup",
                        argument_refs={
                            "key": {
                                "source": "literal",
                                "value": legacy_key,
                            }
                        },
                        output_name="reply",
                        fallback="retry",
                    )
                ],
            )["payload"]
            remaining_steps = source_plan["steps"]
        if remaining_plan.get("mode") == "integrity_failed":
            emit(
                {
                    "type": "agent.failed",
                    "payload": {
                        "code": "EVIDENCE_CONFLICT",
                        "message": "PlanSnapshot 完整性校验失败",
                        "retryable": True,
                        "callId": None,
                    },
                }
            )
            return 0
        outputs: dict[str, dict[str, Any]] = {}
        latest: dict[str, Any] = {}
        source_steps = (
            source_plan.get("steps", [])
            if isinstance(source_plan, dict)
            else [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "id",
                        "action",
                        "capabilityRef",
                        "dependsOn",
                        "argumentRefs",
                        "effect",
                        "idempotencyKey",
                        "outputName",
                        "fallback",
                    }
                }
                for item in [*completed_plan_steps, *remaining_steps]
                if isinstance(item, dict)
            ]
        )
        if isinstance(source_plan, dict):
            accepted_plan = source_plan
            if "[[RECOVERY_NEW_PLAN]]" in original_input:
                changed_plan = {
                    **source_plan,
                    "planId": "untrusted-replacement",
                    "steps": list(reversed(source_plan["steps"])),
                }
                emit({"type": "agent.plan", "payload": changed_plan})
            else:
                emit({"type": "agent.plan", "payload": source_plan})
        elif source_steps:
            accepted_plan = _structured_plan(
                "legacy-replan",
                "人工确认后的受限 legacy replan",
                source_steps,
            )["payload"]
            emit({"type": "agent.plan", "payload": accepted_plan})
        else:
            accepted_plan = None
            emit(
                {
                    "type": "agent.plan",
                    "payload": {"summary": "复用已完成输出，无剩余 Tool 步骤"},
                }
            )
        if "[[RECOVERY_ALWAYS_FAIL]]" in original_input:
            emit(
                {
                    "type": "agent.failed",
                    "payload": {
                        "code": "DEPENDENCY_UNAVAILABLE",
                        "message": "模拟 continuation 持续失败",
                        "retryable": False,
                        "callId": None,
                    },
                }
            )
            return 0
        for item in completed:
            if isinstance(item, dict) and isinstance(item.get("output"), dict):
                latest = item["output"]
                if item.get("planStepId"):
                    outputs[str(item["planStepId"])] = latest
        provenance_result: dict[str, Any] | None = None
        executable_ids = {
            str(item.get("id"))
            for item in remaining_steps
            if isinstance(item, dict)
        }
        executable_steps = [
            step for step in source_steps if str(step.get("id")) in executable_ids
        ]
        if "[[RECOVERY_REPEAT_SUCCEEDED]]" in original_input:
            succeeded_ids = {
                str(item.get("id"))
                for item in completed_plan_steps
                if isinstance(item, dict)
            }
            executable_steps = [
                step
                for step in source_steps
                if str(step.get("id")) in succeeded_ids
            ] + executable_steps
        if (
            "[[RECOVERY_SKIP_DEPENDENCY]]" in original_input
            and len(executable_steps) > 1
        ):
            executable_steps = executable_steps[1:]
        if (
            "[[RECOVERY_REPLAN]]" in original_input
            and isinstance(source_plan, dict)
            and executable_steps
        ):
            replaced = executable_steps[0]
            replacement = {
                **replaced,
                "id": f"{replaced['id']}-replanned",
                "dependsOn": list(replaced.get("dependsOn", [])),
            }
            patch = {
                "schemaVersion": "1",
                "basePlanDigest": continuation["sourcePlanDigest"],
                "baseRevision": continuation["sourcePlanRevision"],
                "baseStepStateDigest": continuation["stepStateDigest"],
                "sourceCheckpointId": continuation["sourceCheckpointId"],
                "replaceStepIds": [
                    str(item["id"]) for item in executable_steps
                ],
                "replacementSteps": [replacement],
                "reason": "受限替换 remaining closure",
            }
            emit({"type": "agent.replan", "payload": patch})
            if "[[RECOVERY_REPLAN_REPEAT]]" in original_input:
                emit({"type": "agent.replan", "payload": patch})
            succeeded_ids = {
                str(item.get("id"))
                for item in completed_plan_steps
                if isinstance(item, dict)
            }
            revised_steps = [
                step
                for step in source_plan["steps"]
                if str(step.get("id")) in succeeded_ids
            ] + [replacement]
            accepted_plan = {
                **source_plan,
                "revision": int(source_plan["revision"]) + 1,
                "summary": patch["reason"],
                "steps": revised_steps,
            }
            source_steps = revised_steps
            executable_steps = [replacement]
        for index, step in enumerate(executable_steps, start=1):
            capability = str(step["capabilityRef"])
            arguments = _tool_arguments(step, original_input, outputs)
            changed_url = _directive(original_input, "RECOVERY_URL", "")
            changed_path = _directive(original_input, "RECOVERY_PATH", "")
            changed_key = _directive(original_input, "RECOVERY_KEY", "")
            if changed_url and "url" in arguments:
                arguments["url"] = changed_url
                parsed_changed = urlsplit(changed_url)
                arguments["pageContract"] = {
                    "contractVersion": "1",
                    "urlPattern": (
                        f"{parsed_changed.scheme}://"
                        f"{parsed_changed.netloc}/*"
                    ),
                    "bodySelector": "#main",
                    "fields": {},
                }
            if changed_path and "path" in arguments:
                Path(changed_path).write_text(
                    "不应被 Host 读取", encoding="utf-8"
                )
                arguments["path"] = changed_path
            if changed_key and "key" in arguments:
                arguments["key"] = changed_key
            if capability == "browser.read":
                url = str(arguments.get("url", ""))
                parsed_url = urlsplit(url)
                page_contract = {
                    "contractVersion": "1",
                    "urlPattern": f"{parsed_url.scheme}://{parsed_url.netloc}/*",
                    "bodySelector": "#main",
                    "fields": {},
                }
                arguments["pageContract"] = page_contract
            call_id = f"continuation-{index}-{step['id']}"
            request_payload: dict[str, Any] = {
                "callId": call_id,
                "planStepId": step["id"],
                "planDigest": (
                    _plan_digest(accepted_plan)
                    if accepted_plan is not None
                    else continuation.get("sourcePlanDigest")
                ),
                "planRevision": (
                    accepted_plan.get("revision", 1)
                    if accepted_plan is not None
                    else continuation.get("sourcePlanRevision", 1)
                ),
                "stepAttempt": len(
                    source_attempts.get(str(step["id"]), [])
                )
                + 1,
                "capabilityRef": capability,
                "action": step["action"],
                "actionKind": (
                    "browser"
                    if capability == "browser.read"
                    else "transform"
                ),
                "arguments": arguments,
                "effect": "none",
                "idempotent": True,
                "idempotencyKey": step.get("idempotencyKey"),
            }
            if capability == "browser.read":
                request_payload.update(
                    {
                        "argumentBindings": {
                            "url": {
                                "source": "user_input",
                                "inputKey": "message",
                            }
                        },
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
                    }
                )
            elif capability == "lookup.table":
                request_payload.update(
                    {
                        "argumentBindings": {
                            "key": (
                                {
                                    "source": "user_input",
                                    "inputKey": "message",
                                }
                                if step.get("argumentRefs", {})
                                .get("key", {})
                                .get("source")
                                == "user_input"
                                else {
                                    "source": "constant",
                                    "value": arguments.get("key"),
                                }
                            )
                        },
                        "replayability": "deterministic",
                        "outputSchema": {
                            "type": "object",
                            "properties": {
                                "table": {"type": "object"},
                                "value": {"type": "string"},
                            },
                            "required": ["table", "value"],
                        },
                    }
                )
            elif capability == "summarize.text":
                request_payload.update(
                    {
                        "actionKind": "agent",
                        "argumentBindings": {
                            "text": {
                                "source": "prior_output",
                                "fromAction": (
                                    f"continuation-{index - 1}-"
                                    f"{step.get('dependsOn', [''])[0]}"
                                ),
                                "fromPath": "content",
                            }
                        },
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
                    }
                )
            emit(
                {
                    "type": "tool.requested",
                    "payload": request_payload,
                }
            )
            provenance_result = read_tool_result(call_id)
            if provenance_result["status"] != "succeeded":
                emit(
                    {
                        "type": "agent.failed",
                        "payload": {
                            "code": provenance_result["error"]["code"],
                            "message": provenance_result["error"]["message"],
                            "retryable": provenance_result["error"].get(
                                "retryable", False
                            ),
                            "callId": call_id,
                        },
                    }
                )
                return 0
            latest = provenance_result.get("output", {})
            outputs[str(step["id"])] = latest
        reply_value = latest.get(
            "reply",
            latest.get("value", latest.get("content", "恢复完成")),
        )
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
                                    "output.reply"
                                    if "reply"
                                    in provenance_result.get("output", {})
                                    else (
                                        "output.content"
                                        if "content"
                                        in provenance_result.get("output", {})
                                        else "output.value"
                                    )
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

    if mode == "reported_failure":
        emit(
            {
                "type": "agent.plan",
                "payload": {"summary": "报告 Runtime 失败"},
            }
        )
        emit(
            {
                "type": "agent.failed",
                "payload": {
                    "code": _directive(
                        args.prompt, "ERROR", "EXECUTION_FAILED"
                    ),
                    "message": "Runtime 自报失败",
                    "retryable": _directive(
                        args.prompt, "RETRYABLE", "true"
                    ).lower()
                    == "true",
                    "callId": (
                        "untrusted-call"
                        if "[[WITH_CALL_ID]]" in args.prompt
                        else None
                    ),
                },
            }
        )
        return 0

    if mode == "hang":
        # 模拟长时间无进展，交给 Host 超时/取消。
        time.sleep(30)
        return 0

    active_plan: dict[str, Any] | None = None
    if mode == "deterministic_fail_after_tool":
        active_plan = _structured_plan(
                "deterministic-lookup",
                "查表后生成结果",
                [
                    _plan_step(
                        "lookup",
                        "lookup.table",
                        action="lookup",
                        argument_refs={
                            "key": {
                                "source": "literal",
                                "value": _clean_message(args.prompt),
                            }
                        },
                        output_name="lookupResult",
                        fallback="retry",
                    )
                ],
            )["payload"]
        emit({"type": "agent.plan", "payload": active_plan})
    elif mode == "planned_browser_summary":
        planned_url_match = re.search(r"https?://[^\s\"}]+", args.prompt)
        planned_url = (
            planned_url_match.group(0)
            if planned_url_match
            else _clean_message(args.prompt)
        )
        planned_parsed_url = urlsplit(planned_url)
        planned_page_contract = {
            "contractVersion": "1",
            "urlPattern": (
                f"{planned_parsed_url.scheme}://"
                f"{planned_parsed_url.netloc}/*"
            ),
            "bodySelector": "#main",
            "fields": {},
        }
        active_plan = _structured_plan(
                "browser-summary",
                "预检、读取网页并总结",
                [
                    _plan_step(
                        "preflight",
                        "lookup.table",
                        action="lookup",
                        argument_refs={
                            "key": {"source": "literal", "value": "q1"}
                        },
                        output_name="preflightResult",
                        fallback="retry",
                    ),
                    _plan_step(
                        "browser",
                        "browser.read",
                        action="read",
                        depends_on=["preflight"],
                        argument_refs={
                            "url": {
                                "source": "user_input",
                                "inputKey": "message",
                                "extract": "url",
                            },
                            "pageContract": {
                                "source": "literal",
                                "value": planned_page_contract,
                            },
                        },
                        output_name="page",
                        fallback="retry",
                    ),
                    _plan_step(
                        "summarize",
                        "summarize.text",
                        action="summarize",
                        depends_on=["browser"],
                        argument_refs={
                            "text": {
                                "source": "step_output",
                                "stepId": "browser",
                                "path": "content",
                            }
                        },
                        output_name="reply",
                    ),
                ],
            )["payload"]
        emit({"type": "agent.plan", "payload": active_plan})
    elif mode == "planned_lookup_transform":
        active_plan = _structured_plan(
                "lookup-transform",
                "查找、转换并输出",
                [
                    _plan_step(
                        "lookup",
                        "lookup.table",
                        action="lookup",
                        argument_refs={
                            "key": {"source": "literal", "value": "q1"}
                        },
                        output_name="source",
                        fallback="retry",
                    ),
                    _plan_step(
                        "transform",
                        "lookup.table",
                        action="transform",
                        depends_on=["lookup"],
                        argument_refs={
                            "key": {"source": "literal", "value": "q2"}
                        },
                        output_name="transformed",
                        fallback="retry",
                    ),
                    _plan_step(
                        "output",
                        "lookup.table",
                        action="output",
                        depends_on=["transform"],
                        argument_refs={
                            "key": {"source": "literal", "value": "q3"}
                        },
                        output_name="reply",
                    ),
                ],
            )["payload"]
        emit({"type": "agent.plan", "payload": active_plan})
    elif mode == "planned_file_transform":
        active_plan = _structured_plan(
                "file-transform",
                "读取文件、转换并输出",
                [
                    _plan_step(
                        "read",
                        "read.local",
                        action="read",
                        argument_refs={
                            "path": {
                                "source": "literal",
                                "value": "input.txt",
                            }
                        },
                        output_name="file",
                        fallback="retry",
                    ),
                    _plan_step(
                        "transform",
                        "summarize.text",
                        action="transform",
                        depends_on=["read"],
                        argument_refs={
                            "text": {
                                "source": "step_output",
                                "stepId": "read",
                                "path": "content",
                            }
                        },
                        output_name="reply",
                    ),
                ],
            )["payload"]
        emit({"type": "agent.plan", "payload": active_plan})
    else:
        emit(
            {
                "type": "agent.plan",
                "payload": {
                    "summary": "解析输入并调用允许的工具",
                    "steps": 2,
                },
            }
        )

    if mode == "planned_lookup_transform":
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-lookup-source",
                    **_planned_request_meta(active_plan, "lookup"),
                    "capabilityRef": "lookup.table",
                    "action": "lookup",
                    "actionKind": "transform",
                    "arguments": {"key": "q1"},
                    "effect": "none",
                    "idempotent": True,
                },
            }
        )
        read_tool_result("call-lookup-source")
        emit(
            {
                "type": "agent.failed",
                "payload": {
                    "code": "DEPENDENCY_UNAVAILABLE",
                    "message": "转换依赖暂时不可用",
                    "retryable": False,
                    "callId": None,
                },
            }
        )
        return 0

    if mode == "planned_file_transform":
        Path("input.txt").write_text(
            _directive(args.prompt, "CONTENT", "文件恢复内容"),
            encoding="utf-8",
        )
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-file-source",
                    **_planned_request_meta(active_plan, "read"),
                    "capabilityRef": "read.local",
                    "action": "read",
                    "actionKind": "file",
                    "arguments": {"path": "input.txt"},
                    "effect": "none",
                    "idempotent": True,
                },
            }
        )
        read_tool_result("call-file-source")
        emit(
            {
                "type": "agent.failed",
                "payload": {
                    "code": "TIMEOUT",
                    "message": "转换阶段暂时超时",
                    "retryable": False,
                    "callId": None,
                },
            }
        )
        return 0

    if mode in {
        "deterministic",
        "deterministic_generative",
        "deterministic_bad_output",
        "deterministic_fail_after_tool",
    }:
        # Agent 只提交 key；整表、选值与固化 evidence 均由 Host Adapter 返回。
        key_match = re.search(r"\bq[1-4]\b", args.prompt)
        key = key_match.group(0) if key_match else _clean_message(args.prompt)
        tool_name = allowed[0] if allowed else "lookup.table"
        emit(
            {
                "type": "tool.requested",
                "payload": {
                    "callId": "call-1",
                    **(
                        _planned_request_meta(active_plan, "lookup")
                        if mode == "deterministic_fail_after_tool"
                        else {}
                    ),
                    "capabilityRef": tool_name,
                    **(
                        {"action": "lookup"}
                        if mode == "deterministic_fail_after_tool"
                        else {}
                    ),
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
                            else f"查询结果：{output.get('value', '未知季度')}"
                            if mode == "deterministic_generative"
                            else output.get("value", "未知季度")
                        )
                    },
                    "provenance": [{"callId": "call-1", "path": "output.value"}],
                    "outputProvenance": (
                        generative_output_provenance(
                            {"reply": f"查询结果：{output.get('value', '未知季度')}"},
                            [tool_result],
                            "fixture-generative-v1",
                        )
                        if mode == "deterministic_generative"
                        else deterministic_output_provenance(
                            "reply", tool_result, "output.value"
                        )
                    ),
                },
            }
        )
        return 0

    if mode in {
        "browser_summary",
        "planned_browser_summary",
        "browser_invalid_completed",
    }:
        if mode == "planned_browser_summary":
            emit(
                {
                    "type": "tool.requested",
                    "payload": {
                        "callId": "call-preflight",
                        **_planned_request_meta(active_plan, "preflight"),
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
                    **(
                        _planned_request_meta(active_plan, "browser")
                        if mode == "planned_browser_summary"
                        else {}
                    ),
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
        fixture_content = _directive(args.prompt, "CONTENT", "")
        if fixture_content:
            Path(path).write_text(fixture_content, encoding="utf-8")
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
        output = _fixture_value(output_schema, "output", reply)

    emit(
        {
            "type": "agent.completed",
            "payload": {"steps": 1, "totalTokens": 20, "output": output},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
