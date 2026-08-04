from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import WorkflowCompiler, WorkflowValidationError
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


def compile_agent_trajectory(
    trajectory: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从真实 Agent 轨迹的成功 action 序列提炼确定节点。

    依次判断每个 action 能否参数化为确定节点：
    - Tool 调用（含 callId）→ 锁定为 Transform 查表节点，参数来自 ``${input.message}``；
    - 无法确定化的动作 → 保留为受限 Agent 节点。

    最后一个确定节点的输出映射到 workflow output。若没有任何可确定化的 action，则退回
    单个受限 Agent 节点（并在分析中标注原因）。
    """

    input_schema = trajectory.get("inputSchema")
    output_schema = trajectory.get("outputSchema")
    if not isinstance(input_schema, dict) or not isinstance(output_schema, dict):
        raise DistillationError("轨迹缺少 inputSchema/outputSchema")
    raw_prompt = str(trajectory.get("prompt", ""))
    message = _clean_message(raw_prompt)
    actions = trajectory.get("actions") or []

    nodes: list[dict[str, Any]] = [{"id": "input", "type": "input", "displayName": "输入"}]
    edges: list[dict[str, str]] = []
    taken = {"input", "output"}
    previous = "input"
    diagnostics: list[dict[str, str]] = []
    deterministic_nodes = 0
    llm_or_agent_nodes = 0
    last_result_ref = "${input.message}"

    for action in actions:
        tool = action.get("capability") or action.get("tool")
        if not isinstance(tool, str) or not tool:
            continue
        result = action.get("result", {})
        arguments = action.get("arguments", {})
        # 只有当 Tool 结果是可查表的字典、且某个参数等于用户输入时，才能安全参数化为
        # 确定节点：把整表冻结进 config，运行时以 ${input.message} 作为 key 选出结果。
        key_field = next(
            (name for name, value in arguments.items() if value == message and message),
            None,
        )
        if not isinstance(result, dict) or key_field is None:
            continue
        node_id = _node_id(f"tool-{tool}", taken)
        node = {
            "id": node_id,
            "type": "transform",
            "displayName": f"锁定 Tool {tool}",
            "input": {"key": "${input.message}"},
            "config": {
                "operation": "lookup",
                "tool": tool,
                # 冻结探索时读取的整张本地表；新输入 key 选出不同结果。
                "table": result,
                "keyField": "key",
                "default": "未知项",
            },
        }
        nodes.append(node)
        edges.append({"source": previous, "target": node_id})
        previous = node_id
        deterministic_nodes += 1
        last_result_ref = f"${{{node_id}.output.value}}"

    if deterministic_nodes == 0:
        # 没有可确定化的 action：保留一个受限 Agent 节点复现探索，但显式标注。
        diagnostics.append(
            {
                "level": "warning",
                "code": "NO_DETERMINISTIC_ACTION",
                "path": "actions",
                "message": "轨迹无可参数化的 Tool 动作，退回受限 Agent 节点",
            }
        )
        agent_id = _node_id("agent", taken)
        nodes.append(
            {
                "id": agent_id,
                "type": "agent",
                "displayName": "受限通用 Agent",
                "input": {"message": "${input.message}"},
                "config": {
                    "outputSchema": output_schema,
                    "maxSteps": trajectory.get("config", {}).get("maxSteps", 6),
                    "maxTotalTokens": trajectory.get("config", {}).get("maxTotalTokens", 30000),
                    "allowedCapabilities": trajectory.get("config", {}).get(
                        "allowedCapabilities", []
                    ),
                    "prompt": trajectory.get("prompt", "完成该任务"),
                },
            }
        )
        edges.append({"source": previous, "target": agent_id})
        previous = agent_id
        llm_or_agent_nodes += 1
        output_mapping = {
            key: f"${{{agent_id}.output.{key}}}"
            for key in output_schema.get("properties", {})
        }
    else:
        # 确定节点链：把最后一个 Tool 结果映射到 output 的每个属性。
        output_mapping = {
            key: last_result_ref for key in output_schema.get("properties", {})
        }

    nodes.append(
        {"id": "output", "type": "output", "displayName": "输出", "input": output_mapping}
    )
    edges.append({"source": previous, "target": "output"})

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
        "llmOrAgentNodes": llm_or_agent_nodes,
        "estimatedLlmCallsBefore": trajectory.get("originalModelCalls", 0),
        "estimatedLlmCallsAfter": llm_or_agent_nodes,
        "toolLocks": [
            action.get("capability") or action.get("tool")
            for action in actions
            if action.get("capability") or action.get("tool")
        ],
        "diagnostics": diagnostics,
        "publishable": not any(item["level"] == "error" for item in diagnostics),
    }
    return definition, analysis


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
    definition, analysis = compile_agent_trajectory(conversation_run.trajectory_json)
    # 保留来源 run id 与 action 摘要，保证固化结果可审计。
    analysis = {
        **analysis,
        "sourceRunId": conversation_run.id,
        "sourceRunType": run_type,
        "actionSummary": [
            {"tool": action.get("capability") or action.get("tool")}
            for action in conversation_run.trajectory_json.get("actions", [])
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
        id=str(uuid4()),
        conversation_run_id=conversation_run.id,
        status="draft_created",
        draft_workflow_id=workflow.id,
        analysis_json=analysis,
    )
    session.add(result)
    await session.flush()
    return result
