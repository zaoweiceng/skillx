from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.routes import PublishRequest, publish_workflow
from app.application.compiler import WorkflowCompiler, digest_json
from app.application.distillation import (
    DistillationError,
    _propose_presentation_policy,
    compile_agent_trajectory,
    distill_conversation_run,
)
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    Run,
    RunEvent,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)

LOOKUP_TABLE = {
    "q1": "第一季度：营收增长 12%",
    "q2": "第二季度：营收增长 8%",
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}

INPUT_SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


def _base_trajectory(actions: list[dict], *, message: str = "q1") -> dict:
    return {
        "runType": "agent_run",
        "name": "distilled",
        "prompt": message,
        "inputSchema": INPUT_SCHEMA,
        "outputSchema": OUTPUT_SCHEMA,
        "actions": actions,
        "output": {"reply": LOOKUP_TABLE.get(message, "")},
        "outputProvenance": {
            "reply": {
                "source": "action_output",
                "actionId": actions[-1].get("callId", "missing"),
                "path": "value",
            }
        },
        "config": {"maxSteps": 6, "maxTotalTokens": 30000, "allowedCapabilities": []},
        "originalModelCalls": 1,
    }


def _lookup_action(message: str = "q1") -> dict:
    # 带固化证据的 transform lookup 动作（operation/table）。
    return {
        "callId": "call-1",
        "kind": "transform",
        "capability": "lookup.table",
        "arguments": {"key": message},
        "result": dict(LOOKUP_TABLE),
        "effect": "none",
        "idempotent": True,
        "replayability": "deterministic",
        "observations": {"operation": "lookup", "table": dict(LOOKUP_TABLE)},
    }


def test_lookup_action_with_evidence_becomes_deterministic_node() -> None:
    first, analysis = compile_agent_trajectory(_base_trajectory([_lookup_action()]))

    second, _ = compile_agent_trajectory(_base_trajectory([_lookup_action()]))
    assert digest_json(first) == digest_json(second)
    assert analysis["publishable"] is True
    assert analysis["directNodes"] == 1
    assert analysis["agentNodes"] == 0
    node = first["spec"]["nodes"][1]
    assert node["type"] == "transform"
    assert node["config"]["operation"] == "lookup"


def test_plain_tool_dict_result_is_not_auto_lookup() -> None:
    # 真实动态结果 {title, content}，无固化证据：绝不误编译为 lookup，保留为 Agent 节点。
    action = {
        "callId": "call-1",
        "kind": "tool",
        "capability": "web.fetch",
        "arguments": {"url": "q1"},
        "result": {"title": "Q1", "content": "动态正文"},
    }
    definition, analysis = compile_agent_trajectory(_base_trajectory([action]))
    node = definition["spec"]["nodes"][1]
    assert node["type"] == "agent"
    assert analysis["directNodes"] == 0
    assert analysis["agentNodes"] == 1
    # 未被误编译为查表。
    assert all(n["type"] != "transform" for n in definition["spec"]["nodes"])


def test_mixed_trajectory_preserves_all_actions_in_order() -> None:
    # 确定 lookup + 未识别动作（保留 Agent），两者顺序与依赖都保留，不丢步骤。
    lookup = _lookup_action()
    unknown = {
        "callId": "call-2",
        "kind": "tool",
        "capability": "summarize.text",
        "arguments": {"text": "some"},
        "result": {"summary": "s"},
    }
    definition, analysis = compile_agent_trajectory(_base_trajectory([lookup, unknown]))
    node_types = [n["type"] for n in definition["spec"]["nodes"]]
    # input + transform(lookup) + agent(unknown) + output，动作数不减少。
    assert node_types == ["input", "transform", "agent", "output"]
    assert analysis["directNodes"] == 1
    assert analysis["agentNodes"] == 1


def test_locked_mcp_capability_becomes_mcp_node() -> None:
    action = {
        "callId": "call-1",
        "kind": "mcp",
        "capability": "weather.get",
        "capabilityRef": {
            "registryId": "snap-123",
            "version": "1",
            "digest": "sha256:abc",
        },
        "arguments": {"city": "q1"},
        "result": {"temp": 20},
        "effect": "none",
        "idempotent": True,
    }
    definition, analysis = compile_agent_trajectory(_base_trajectory([action]))
    node = definition["spec"]["nodes"][1]
    assert node["type"] == "mcp"
    assert node["config"]["mcpToolSnapshotId"] == "snap-123"
    assert analysis["capabilityLocks"][0]["digest"] == "sha256:abc"


def test_unlocked_mcp_capability_stays_agent() -> None:
    # 没有能力锁（缺 registryId/version/digest）：不得确定化为 MCP 节点。
    action = {
        "callId": "call-1",
        "kind": "mcp",
        "capability": "weather.get",
        "arguments": {"city": "q1"},
        "result": {"temp": 20},
    }
    definition, _ = compile_agent_trajectory(_base_trajectory([action]))
    assert definition["spec"]["nodes"][1]["type"] == "agent"


def test_missing_call_id_analysis_fails() -> None:
    action = {"kind": "tool", "capability": "x", "arguments": {}, "result": {}}
    with pytest.raises(DistillationError):
        compile_agent_trajectory(_base_trajectory([action]))


def test_valid_proposal_participates_in_adjudication() -> None:
    from app.application.distillation_ir import DistillationProposal

    # Agent 建议把 lookup 动作保留为 Agent 节点（proposed_strategy=agent）：
    # Compiler 尊重该判断，即使动作本可确定化为 transform。
    proposal = DistillationProposal.model_validate(
        {"nodes": [{"nodeId": "n1", "actionIds": ["call-1"], "strategy": "agent"}]}
    )
    definition, analysis = compile_agent_trajectory(
        _base_trajectory([_lookup_action()]), proposal
    )
    assert definition["spec"]["nodes"][1]["type"] == "agent"
    assert analysis["agentNodes"] == 1


def test_proposal_groups_multiple_actions_into_one_agent_node() -> None:
    from app.application.distillation_ir import DistillationProposal

    first = _lookup_action()
    second = {
        "callId": "call-2",
        "kind": "agent",
        "capability": "summarize.text",
        "arguments": {"text": "dynamic"},
        "argumentBindings": {
            "text": {
                "source": "prior_output",
                "fromAction": "call-1",
                "fromPath": "value",
            }
        },
        "result": {"reply": "summary"},
        "outputSchema": OUTPUT_SCHEMA,
    }
    trajectory = _base_trajectory([first, second])
    trajectory["outputProvenance"]["reply"] = {
        "source": "action_output",
        "actionId": "call-2",
        "path": "reply",
    }
    proposal = DistillationProposal.model_validate(
        {
            "nodes": [
                {
                    "nodeId": "combined",
                    "actionIds": ["call-1", "call-2"],
                    "strategy": "agent",
                }
            ]
        }
    )
    definition, analysis = compile_agent_trajectory(trajectory, proposal)
    business_nodes = definition["spec"]["nodes"][1:-1]
    assert len(business_nodes) == 1
    assert business_nodes[0]["id"] == "combined"
    assert business_nodes[0]["type"] == "agent"
    assert business_nodes[0]["config"]["allowedCapabilities"] == [
        "lookup.table",
        "summarize.text",
    ]
    assert analysis["agentNodes"] == 1


def test_proposal_not_covering_every_action_is_rejected() -> None:
    from app.application.distillation_ir import DistillationProposal

    lookup = _lookup_action()
    second = {
        "callId": "call-2",
        "kind": "tool",
        "capability": "summarize.text",
        "arguments": {"text": "x"},
        "result": {"summary": "y"},
    }
    # proposal 只覆盖 call-1，遗漏 call-2 → Compiler 拒绝，不静默换成错误图。
    proposal = DistillationProposal.model_validate(
        {"nodes": [{"nodeId": "n1", "actionIds": ["call-1"], "strategy": "deterministic"}]}
    )
    with pytest.raises(DistillationError):
        compile_agent_trajectory(_base_trajectory([lookup, second]), proposal)


async def test_distillation_rejects_imported_trajectory() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="导入")
        session.add(conversation)
        await session.flush()
        imported = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            run_type="trajectory_import",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=_base_trajectory([_lookup_action()]),
        )
        session.add(imported)
        await session.flush()
        with pytest.raises(DistillationError):
            await distill_conversation_run(session, imported)


async def test_distilled_draft_reuse_varies_with_new_input() -> None:
    """固化后用不同输入运行，结果随输入变化且零模型调用（真实提炼）。"""

    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="复用")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            run_type="agent_run",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=_base_trajectory([_lookup_action("q1")], message="q1"),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        draft_workflow_id = result.draft_workflow_id

    async with SessionLocal() as session:
        workflow = await session.get(Workflow, draft_workflow_id)
        published = await publish_workflow(
            draft_workflow_id,
            PublishRequest(
                draftRevision=workflow.draft_revision,
                draftDigest=WorkflowCompiler().compile(
                    workflow.draft_json
                ).digest,
            ),
            session,
        )
    version_id = published["id"]

    async with SessionLocal() as session, session.begin():
        version = await session.get(WorkflowVersion, version_id)
        run = await Orchestrator().create_run(session, version, {"message": "q2"})
        run_id = run.id
    worker = WorkerService("distill-reuse-worker")
    for _ in range(3):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.output_json == {"reply": LOOKUP_TABLE["q2"]}
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
    assert not {"agent.completed", "llm.call.completed"} & event_types


async def test_distillation_creates_editable_draft_without_publishing() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="草稿")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=_base_trajectory([_lookup_action()]),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        workflow = await session.get(Workflow, result.draft_workflow_id)
        assert workflow is not None
        versions = await session.scalar(
            select(func.count())
            .select_from(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow.id)
        )
        assert versions == 0


def _definition_with_output(output_schema: dict) -> dict:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "任务"},
        "spec": {
            "inputSchema": INPUT_SCHEMA,
            "outputSchema": output_schema,
            "nodes": [],
            "edges": [],
        },
    }


def test_presentation_proposal_simple_structured_uses_template() -> None:
    """简单结构化结果（标量字段，直接面向用户）→ 可选模板文本；无任何领域判断。"""

    definition = _definition_with_output(
        {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "condition": {"type": "string"},
                "temperature": {"type": "number"},
            },
        }
    )
    proposal = _propose_presentation_policy(definition, machine_only=False)
    assert proposal["enabled"] is True
    assert proposal["audience"] == "end_user"
    assert proposal["producer"] == "template"
    # 模板只由 output schema 字段拼接而成，不含任何领域字面量。
    assert "{city}" in proposal["template"] and "{temperature}" in proposal["template"]
    assert proposal["reason"] and proposal["evidence"]


def test_presentation_proposal_complex_result_uses_llm() -> None:
    """复杂结果（嵌套对象/数组，如网页/长文本总结）→ LLM/Agent Presentation。"""

    definition = _definition_with_output(
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "string"}},
                "meta": {"type": "object"},
            },
        }
    )
    proposal = _propose_presentation_policy(definition, machine_only=False)
    assert proposal["enabled"] is True
    assert proposal["producer"] == "llm"
    assert "prompt" in proposal


def test_presentation_proposal_machine_result_is_disabled() -> None:
    """机器消费/组合中间结果（数据转换、Fragment）→ Presentation 默认关闭。"""

    definition = _definition_with_output(
        {
            "type": "object",
            "properties": {"records": {"type": "array"}, "count": {"type": "integer"}},
        }
    )
    proposal = _propose_presentation_policy(definition, machine_only=True)
    assert proposal["enabled"] is False
    assert proposal["audience"] == "internal"


def test_presentation_no_domain_specific_branch_in_generic_runtime() -> None:
    """静态检查：通用固化/展示运行时不得出现天气等领域专用分支或字面量。"""

    import re as _re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app" / "application"
    forbidden = ("天气", "weather", "温度", "℃")
    for name in ("distillation.py", "product_history.py"):
        text = (root / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name} 不得包含领域专用字面量 {token!r}"
    # Presentation 决策只依据数据流形态与节点类型，不得按能力名称/tag 分支到领域特判。
    distillation_text = (root / "distillation.py").read_text(encoding="utf-8")
    assert not _re.search(
        r'capability\.name\s*==|\.tags\s*==|in\s+capability\.tags', distillation_text
    )


async def test_distillation_freezes_generic_presentation_proposal() -> None:
    """固化产出通用 presentationPolicy 与可审阅 proposal，且不含领域专用分支。"""

    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="Presentation 提议")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            run_type="agent_run",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=_base_trajectory([_lookup_action("q1")], message="q1"),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        workflow = await session.get(Workflow, result.draft_workflow_id)
        metadata = workflow.draft_json["metadata"]
        # 固化阶段按数据流通用提出 Presentation，并把可审阅提议写入 analysis。
        assert "presentationPolicy" in metadata
        proposal = result.analysis_json["presentationPolicyProposal"]
        assert proposal["source"] == "distillation_dataflow"
        assert proposal["reason"] and proposal["confidence"] > 0
