import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routes import (
    AgentRunCreate,
    distill_run,
    get_conversation_run,
    list_conversation_run_events,
    start_agent_run,
)
from app.application.agent_run import AgentRunConfig, AgentRunService
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(settings, "traex_allowed_executables", [Path(sys.executable).name])


async def _conversation() -> str:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="通用 Agent")
        session.add(conversation)
        await session.flush()
        return conversation.id


async def test_agent_run_service_creates_agent_run_with_events(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await AgentRunService().start_agent_run(
            session,
            conversation,
            "完成任务 [[MODE:ok]][[REPLY:分类完成]]",
            config=AgentRunConfig(
                output_schema={
                    "type": "object",
                    "properties": {"reply": {"type": "string"}},
                    "required": ["reply"],
                    "additionalProperties": False,
                },
                allowed_capabilities=("search.web",),
            ),
        )
        run_id = run.id
    async with SessionLocal() as session:
        stored = await session.get(ConversationRun, run_id)
        assert stored is not None
        assert stored.status == "succeeded"
        assert stored.run_type == "agent_run"
        assert stored.output_json == {"reply": "分类完成"}
        assert stored.trajectory_json["steps"][0]["kind"] == "agent"
        event_types = set(
            await session.scalars(
                select(ConversationRunEvent.type).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
        assert {"agent.run.created", "agent.plan", "agent.run.succeeded"} <= event_types


async def test_agent_run_service_rejects_unauthorized_tool(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await AgentRunService().start_agent_run(
            session,
            conversation,
            "完成任务 [[MODE:unauthorized_tool]]",
            config=AgentRunConfig.default(),
        )
        assert run.status == "failed"
        assert run.error_json["code"] == "PERMISSION_DENIED"


async def test_agent_run_service_rejects_missing_output(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await AgentRunService().start_agent_run(
            session,
            conversation,
            "完成任务 [[MODE:no_output]]",
            config=AgentRunConfig.default(),
        )
        assert run.status == "failed"
        assert run.error_json["code"] == "EXECUTION_FAILED"


async def test_agent_endpoint_starts_run_and_is_distillable(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session:
        response = await start_agent_run(
            conversation_id,
            AgentRunCreate(content="完成任务 [[MODE:ok]][[REPLY:探索结果]]"),
            session,
        )
    assert response["status"] == "succeeded"
    assert response["assistant"]["type"] == "agent_run"
    conversation_run_id = response["conversationRunId"]

    # 事件可通过 API 恢复（不依赖前端本地 state）。
    async with SessionLocal() as session:
        events = await list_conversation_run_events(conversation_run_id, 0, session)
    assert any(item["type"] == "agent.run.succeeded" for item in events)

    async with SessionLocal() as session:
        summary = await get_conversation_run(conversation_run_id, session)
    assert summary["runType"] == "agent_run"
    assert summary["output"] == {"reply": "探索结果"}

    # agent_run 可直接进入固化草稿。
    async with SessionLocal() as session:
        distilled = await distill_run(conversation_run_id, session)
    assert distilled["status"] == "draft_created"
    assert distilled["analysis"]["sourceRunType"] == "agent_run"


async def test_agent_run_not_configured_records_failure() -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await AgentRunService().start_agent_run(
            session,
            conversation,
            "任意任务",
            config=AgentRunConfig.default(),
        )
        assert run.status == "failed"
        assert run.error_json["code"] == "MODEL_NOT_CONFIGURED"
