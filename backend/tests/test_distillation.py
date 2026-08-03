from uuid import uuid4

from sqlalchemy import func, select

from app.application.compiler import digest_json
from app.application.distillation import compile_trajectory, distill_conversation_run
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


def fixed_trajectory() -> dict:
    return {
        "name": "distilled-greeting",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "steps": [
            {
                "id": "format",
                "kind": "transform",
                "displayName": "格式化",
                "input": {"name": "${input.name}"},
                "config": {"operation": "template", "template": "你好，{name}"},
            }
        ],
        "outputMapping": {"message": "${format.output.text}"},
        "originalModelCalls": 3,
    }


def test_fixed_trajectory_produces_stable_zero_llm_draft() -> None:
    first, analysis = compile_trajectory(fixed_trajectory())
    second, _ = compile_trajectory(fixed_trajectory())

    assert digest_json(first) == digest_json(second)
    assert analysis["publishable"] is True
    assert analysis["estimatedLlmCallsBefore"] == 3
    assert analysis["estimatedLlmCallsAfter"] == 0


def test_unknown_step_stays_agent_and_is_not_publishable() -> None:
    trajectory = fixed_trajectory()
    trajectory["steps"][0] = {
        "id": "dynamic",
        "kind": "dynamic-planning",
        "summary": "动态选择工具",
        "outputSchema": {"type": "object"},
        "allowedCapabilities": ["tool-a"],
    }
    trajectory["outputMapping"] = {"message": "${dynamic.output.message}"}

    definition, analysis = compile_trajectory(trajectory)

    assert definition["spec"]["nodes"][1]["type"] == "agent"
    assert analysis["publishable"] is False
    assert analysis["diagnostics"][0]["code"] == "UNSTABLE_STEP"


async def test_distillation_creates_editable_draft_without_publishing() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="固化测试")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            input_json={"name": "Alice"},
            output_json={"message": "你好，Alice"},
            trajectory_json=fixed_trajectory(),
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
