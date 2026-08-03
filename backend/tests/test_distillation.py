from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.routes import publish_workflow
from app.application.compiler import digest_json
from app.application.distillation import (
    DistillationError,
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

# 真实 Agent 轨迹：一次确定性 Tool 调用读取整张本地表，参数来自用户输入。
LOOKUP_TABLE = {
    "q1": "第一季度：营收增长 12%",
    "q2": "第二季度：营收增长 8%",
}


def agent_trajectory(*, message: str = "q1") -> dict:
    return {
        "runType": "agent_run",
        "name": "distilled-lookup",
        "prompt": message,
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
            "additionalProperties": False,
        },
        "actions": [
            {
                "callId": "call-1",
                "tool": "lookup.table",
                "arguments": {"key": message},
                "result": dict(LOOKUP_TABLE),
            }
        ],
        "output": {"reply": LOOKUP_TABLE[message]},
        "config": {"maxSteps": 6, "maxTotalTokens": 30000, "allowedCapabilities": ["lookup.table"]},
        "originalModelCalls": 1,
    }


def test_tool_action_becomes_deterministic_lookup_node() -> None:
    first, analysis = compile_agent_trajectory(agent_trajectory())
    second, _ = compile_agent_trajectory(agent_trajectory())

    # 提炼稳定、可发布、零模型调用，且锁定了 Tool。
    assert digest_json(first) == digest_json(second)
    assert analysis["publishable"] is True
    assert analysis["directNodes"] == 1
    assert analysis["llmOrAgentNodes"] == 0
    assert analysis["estimatedLlmCallsBefore"] == 1
    assert analysis["estimatedLlmCallsAfter"] == 0
    assert analysis["toolLocks"] == ["lookup.table"]
    # 第二个节点是确定性 lookup Transform，不是 Agent。
    node = first["spec"]["nodes"][1]
    assert node["type"] == "transform"
    assert node["config"]["operation"] == "lookup"


def test_trajectory_without_parameterizable_action_falls_back_to_agent() -> None:
    trajectory = agent_trajectory()
    # 参数与用户输入不相等：无法安全参数化，退回受限 Agent 节点。
    trajectory["actions"][0]["arguments"] = {"key": "hardcoded"}
    definition, analysis = compile_agent_trajectory(trajectory)
    assert definition["spec"]["nodes"][1]["type"] == "agent"
    assert analysis["llmOrAgentNodes"] == 1
    assert analysis["diagnostics"][0]["code"] == "NO_DETERMINISTIC_ACTION"


async def test_distillation_creates_editable_draft_without_publishing() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="固化测试")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=agent_trajectory(),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        workflow = await session.get(Workflow, result.draft_workflow_id)
        assert workflow is not None
        assert result.status == "draft_created"
        versions = await session.scalar(
            select(func.count())
            .select_from(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow.id)
        )
        assert versions == 0


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
            trajectory_json=agent_trajectory(),
        )
        session.add(imported)
        await session.flush()
        with pytest.raises(DistillationError):
            await distill_conversation_run(session, imported)


async def test_distilled_draft_reuse_varies_with_new_input() -> None:
    """固化后用不同输入运行，结果随输入变化且零模型调用（证明真实提炼）。"""

    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="固化复用")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            run_type="agent_run",
            input_json={"message": "q1"},
            output_json={"reply": LOOKUP_TABLE["q1"]},
            trajectory_json=agent_trajectory(message="q1"),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        draft_workflow_id = result.draft_workflow_id
        assert result.analysis_json["sourceRunId"] == conversation_run.id

    async with SessionLocal() as session:
        published = await publish_workflow(draft_workflow_id, session)
    version_id = published["id"]

    # 用与探索时不同的输入（q2）运行发布版本：结果应随输入变化。
    async with SessionLocal() as session, session.begin():
        version = await session.get(WorkflowVersion, version_id)
        assert version is not None
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
        # 新输入 q2 选出不同结果，未运行任何 Agent/LLM。
        assert completed.output_json == {"reply": LOOKUP_TABLE["q2"]}
        event_types = set(
            await session.scalars(
                select(RunEvent.type).where(RunEvent.run_id == run_id)
            )
        )
    assert not {"agent.completed", "llm.call.completed"} & event_types
