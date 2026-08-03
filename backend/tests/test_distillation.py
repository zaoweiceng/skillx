from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.routes import publish_workflow
from app.application.compiler import digest_json
from app.application.distillation import (
    DistillationError,
    compile_trajectory,
    distill_conversation_run,
)
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    Run,
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
            input_json={"name": "Alice"},
            output_json={"message": "你好，Alice"},
            trajectory_json=fixed_trajectory(),
        )
        session.add(imported)
        await session.flush()
        with pytest.raises(DistillationError):
            await distill_conversation_run(session, imported)


async def test_distilled_draft_publishes_and_reuses_with_new_input() -> None:
    # 1) 从服务端成功运行生成草稿（保留来源 run id）。
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="固化复用")
        session.add(conversation)
        await session.flush()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="succeeded",
            run_type="agent_run",
            input_json={"name": "Alice"},
            output_json={"message": "你好，Alice"},
            trajectory_json=fixed_trajectory(),
        )
        session.add(conversation_run)
        await session.flush()
        result = await distill_conversation_run(session, conversation_run)
        draft_workflow_id = result.draft_workflow_id
        assert result.analysis_json["sourceRunId"] == conversation_run.id

    # 2) 人工发布草稿为不可变版本（系统不自动发布）。
    async with SessionLocal() as session:
        published = await publish_workflow(draft_workflow_id, session)
    version_id = published["id"]

    # 3) 用不同输入启动发布版本并验证复用输出。
    async with SessionLocal() as session, session.begin():
        version = await session.get(WorkflowVersion, version_id)
        assert version is not None
        run = await Orchestrator().create_run(session, version, {"name": "Bob"})
        run_id = run.id
    worker = WorkerService("distill-reuse-worker")
    for _ in range(3):
        assert await worker.run_once()
    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.output_json == {"message": "你好，Bob"}
