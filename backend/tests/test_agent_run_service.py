import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routes import (
    AgentRunCreate,
    cancel_conversation_run,
    distill_run,
    get_conversation_run,
    list_conversation_run_events,
    start_agent_run,
)
from app.application.agent_run import (
    AgentRunConfig,
    AgentRunWorker,
    enqueue_agent_run,
    resolve_effective_capabilities,
)
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
    utcnow,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(settings, "traex_allowed_executables", [Path(sys.executable).name])
    monkeypatch.setattr(settings, "agent_host_capabilities", ["search.web", "lookup.table"])


async def _conversation() -> str:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="通用 Agent")
        session.add(conversation)
        await session.flush()
        return conversation.id


async def _enqueue(conversation_id: str, content: str, **kwargs) -> str:
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await enqueue_agent_run(session, conversation, content, **kwargs)
        return run.id


async def _drive(run_id: str) -> ConversationRun:
    worker = AgentRunWorker("test-agent-worker")
    for _ in range(3):
        if not await worker.poll_once():
            break
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        assert run is not None
        return run


def test_effective_capabilities_is_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_host_capabilities", ["search.web"])
    effective, denied = resolve_effective_capabilities(("search.web", "banned.tool"))
    assert effective == ["search.web"]
    assert denied == ["banned.tool"]


async def test_enqueue_creates_queued_run_without_executing(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id, "完成任务 [[MODE:ok]][[REPLY:分类完成]]"
    )
    # 入队即持久化，尚未执行：状态 queued，仅有 created 事件。
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        assert run.status == "queued"
        events = list(
            await session.scalars(
                select(ConversationRunEvent.type).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
        assert events == ["agent.run.created"]


async def test_worker_executes_queued_run_to_success(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:ok]][[REPLY:分类完成]]")
    run = await _drive(run_id)
    assert run.status == "succeeded"
    assert run.run_type == "agent_run"
    assert run.output_json == {"reply": "分类完成"}
    async with SessionLocal() as session:
        event_types = set(
            await session.scalars(
                select(ConversationRunEvent.type).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
        assert {"agent.run.created", "agent.run.started", "agent.plan", "agent.run.succeeded"} <= (
            event_types
        )


async def test_worker_rejects_unauthorized_tool(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:unauthorized_tool]]")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "PERMISSION_DENIED"


async def test_worker_rejects_missing_output(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:no_output]]")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "EXECUTION_FAILED"


async def test_worker_enforces_step_budget(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "完成任务 [[MODE:many_steps]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            max_steps=3,
            allowed_capabilities=("search.web",),
        ),
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "STEP_BUDGET_EXCEEDED"


async def test_worker_enforces_token_budget(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "完成任务 [[MODE:big_tokens]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            max_total_tokens=1000,
            allowed_capabilities=("search.web",),
        ),
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "TOKEN_BUDGET_EXCEEDED"


async def test_running_events_visible_before_terminal(traex_stub: None) -> None:
    """另一个连接在运行完成前能看到 committed 的运行中事件。"""

    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "长任务 [[MODE:hang]]")
    worker = AgentRunWorker("running-visibility-worker")
    task = asyncio.create_task(worker.poll_once())
    try:
        # 轮询独立连接，直到出现 running 状态与运行中事件。
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with SessionLocal() as session:
                run = await session.get(ConversationRun, run_id)
                events = list(
                    await session.scalars(
                        select(ConversationRunEvent.type).where(
                            ConversationRunEvent.conversation_run_id == run_id
                        )
                    )
                )
            if run.status == "running" and "agent.run.started" in events:
                break
        assert run.status == "running"
        assert "agent.run.started" in events
        # 请求取消，Worker 应把 hang 子进程终止并写入 cancelled 终态。
        async with SessionLocal() as session, session.begin():
            from app.application.agent_run import request_cancellation

            await request_cancellation(session, run_id)
    finally:
        await asyncio.wait_for(task, timeout=40)
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
    assert run.status == "cancelled"


async def test_cancel_queued_run_never_executes(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:x]]")
    async with SessionLocal() as session:
        run = await cancel_conversation_run(run_id, session)
    assert run["status"] == "cancelled"
    # Worker 领取时不再执行已取消的 run。
    worker = AgentRunWorker("cancel-worker")
    processed = await worker.poll_once()
    assert processed is False
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
    assert run.status == "cancelled"


async def test_recover_expired_lease_marks_unknown_outcome(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:x]]")
    # 模拟一个被领取但租约过期、结果未知的 running 运行。
    async with SessionLocal() as session, session.begin():
        run = await session.get(ConversationRun, run_id)
        run.status = "running"
        run.lease_owner = "dead-worker"
        run.lease_expires_at = utcnow() - timedelta(hours=1)
    worker = AgentRunWorker("recover-worker")
    async with SessionLocal() as session, session.begin():
        recovered = await worker.recover_expired(session)
    assert recovered == 1
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "UNKNOWN_OUTCOME"


async def test_agent_endpoint_returns_202_and_is_distillable(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session:
        response = await start_agent_run(
            conversation_id,
            AgentRunCreate(content="完成任务 [[MODE:ok]][[REPLY:探索结果]]"),
            session,
        )
    assert response["status"] == "queued"
    conversation_run_id = response["conversationRunId"]

    run = await _drive(conversation_run_id)
    assert run.status == "succeeded"

    async with SessionLocal() as session:
        events = await list_conversation_run_events(conversation_run_id, 0, session)
    assert any(item["type"] == "agent.run.succeeded" for item in events)

    async with SessionLocal() as session:
        summary = await get_conversation_run(conversation_run_id, session)
    assert summary["runType"] == "agent_run"
    assert summary["output"] == {"reply": "探索结果"}

    async with SessionLocal() as session:
        distilled = await distill_run(conversation_run_id, session)
    assert distilled["status"] == "draft_created"
    assert distilled["analysis"]["sourceRunType"] == "agent_run"


async def test_run_not_configured_records_failure() -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任意任务")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "MODEL_NOT_CONFIGURED"
