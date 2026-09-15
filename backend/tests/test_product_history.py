import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.api.routes import MessageCreate, create_message, list_runs
from app.application.compiler import WorkflowCompiler
from app.application.product_history import (
    PresentationDriver,
    record_model_usage,
    turn_view,
    usage_summary,
)
from app.application.structured_generation import runtime_snapshot_for_definition
from app.config import get_settings
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import (
    Component,
    Conversation,
    ConversationRun,
    ConversationTurn,
    ModelUsage,
    Run,
    RunEvent,
    RunPresentation,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
)
from tests.fixtures import deterministic_workflow

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


async def test_disabled_conversation_fences_workflow_capabilities() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(
            id=str(uuid4()),
            title="关闭工作流",
            workflow_mode="disabled",
        )
        session.add(conversation)

    async with SessionLocal() as session:
        response = await create_message(
            conversation.id,
            MessageCreate(content="上海天气如何"),
            session,
        )

    assert response["assistant"]["type"] == "agent_run"
    assert response["assistant"]["workflowModeSnapshot"] == "disabled"
    async with SessionLocal() as session:
        run = await session.scalar(select(ConversationRun))
        turn = await session.scalar(select(ConversationTurn))
    assert run is not None
    assert turn is not None
    assert run.turn_id == turn.id
    assert turn.workflow_mode_snapshot == "disabled"
    assert not any(
        item.startswith("workflow.")
        for item in run.config_json["allowedCapabilities"]
    )


async def test_usage_ledger_deduplicates_call_attempt_and_reports_partial() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="usage")
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
            accepted_at=utcnow(),
        )
        session.add_all([conversation, turn])
        await session.flush()
        await record_model_usage(
            session,
            call_id="call-1",
            attempt=1,
            stage="workflow_selection",
            turn_id=turn.id,
            total_tokens=12,
            usage_complete=True,
        )
        await record_model_usage(
            session,
            call_id="call-1",
            attempt=1,
            stage="workflow_selection",
            turn_id=turn.id,
            total_tokens=99,
            usage_complete=True,
        )
        await record_model_usage(
            session,
            call_id="call-2",
            attempt=1,
            stage="presentation",
            turn_id=turn.id,
            usage_complete=False,
        )
        summary = await usage_summary(session, turn.id)

    assert summary["state"] == "partial"
    assert summary["totalTokens"] == 12
    async with SessionLocal() as session:
        rows = (await session.scalars(select(ModelUsage))).all()
    assert len(rows) == 2


async def test_turn_time_is_rfc3339_utc_after_sqlite_round_trip() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="timezone")
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
            accepted_at=datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        )
        session.add_all([conversation, turn])

    async with SessionLocal() as session:
        persisted = await session.get(ConversationTurn, turn.id)
        assert persisted is not None
        view = await turn_view(session, persisted)

    assert view["acceptedAt"] == "2026-08-10T06:00:00Z"
    assert view["usage"]["state"] == "pending"
    assert view["usage"]["totalTokens"] is None


async def test_template_presentation_is_persisted_without_changing_run_output() -> None:
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    now = utcnow()
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="展示",
            draft_revision=1,
            draft_json=definition,
        )
        conversation = Conversation(id=str(uuid4()), title="展示")
        session.add_all([workflow, conversation])
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={"executionClass": "zero_llm"},
            presentation_policy_json={
                "enabled": True,
                "audience": "end_user",
                "producer": "template",
                "template": "问候：{message}",
                "language": "zh-CN",
                "maxLength": 200,
            },
        )
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
            accepted_at=now,
        )
        session.add_all([version, turn])
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            turn_id=turn.id,
            status="succeeded",
            input_json={"name": "Alice"},
            output_json={"message": "你好，Alice"},
            started_at=now,
            finished_at=now,
        )
        session.add(run)

    assert await PresentationDriver().poll_once()
    async with SessionLocal() as session:
        presentation = await session.scalar(select(RunPresentation))
        persisted_run = await session.get(Run, run.id)
        persisted_turn = await session.get(ConversationTurn, turn.id)
        events = (
            await session.scalars(
                select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
            )
        ).all()
    assert presentation is not None
    assert presentation.status == "completed"
    assert presentation.text == "问候：你好，Alice"
    assert persisted_run is not None
    assert persisted_run.output_json == {"message": "你好，Alice"}
    assert persisted_turn is not None and persisted_turn.status == "succeeded"
    assert [item.type for item in events] == [
        "presentation.started",
        "presentation.delta",
        "presentation.completed",
    ]
    assert not await PresentationDriver().poll_once()


async def test_llm_presentation_unavailable_does_not_switch_agent(
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_runtime_type", "fixture")
    monkeypatch.setattr(settings, "agent_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(
        settings, "agent_allowed_executables", [Path(sys.executable).name]
    )
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    now = utcnow()
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="fallback-presentation",
            draft_revision=1,
            draft_json=definition,
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={"executionClass": "hybrid"},
            presentation_policy_json={
                "enabled": True,
                "audience": "end_user",
                "producer": "llm",
                "prompt": "整理结果",
                "language": "zh-CN",
                "maxLength": 200,
            },
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="succeeded",
            input_json={"name": "Alice"},
            output_json={"message": "你好，Alice"},
            runtime_snapshot_json=runtime_snapshot_for_definition(
                WorkflowDefinition.model_validate(definition)
            ),
            started_at=now,
            finished_at=now,
        )
        session.add(run)

    assert await PresentationDriver().poll_once()
    async with SessionLocal() as session:
        presentation = await session.scalar(select(RunPresentation))
        usage = await session.scalar(
            select(ModelUsage).where(ModelUsage.presentation_id == presentation.id)
        )
    assert presentation is not None
    assert presentation.status == "failed"
    assert presentation.error_json["code"] == "MODEL_RUNTIME_UNAVAILABLE"
    assert usage is None


async def test_run_history_filters_on_server() -> None:
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="history",
            draft_revision=1,
            draft_json=definition,
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        first = Component(
            id=str(uuid4()),
            name="first",
            workflow_id=workflow.id,
            default_version_id=version.id,
        )
        second = Component(
            id=str(uuid4()),
            name="second",
            workflow_id=workflow.id,
            default_version_id=version.id,
        )
        session.add_all([first, second])
        await session.flush()
        session.add_all(
            [
                Run(
                    id=str(uuid4()),
                    workflow_version_id=version.id,
                    component_id=first.id,
                    status="failed",
                    input_json={},
                ),
                Run(
                    id=str(uuid4()),
                    workflow_version_id=version.id,
                    component_id=second.id,
                    status="succeeded",
                    input_json={},
                    output_json={},
                ),
            ]
        )

    async with SessionLocal() as session:
        page = await list_runs(
            component_id=first.id,
            workflow_version_id=None,
            status="failed",
            cursor=None,
            limit=20,
            session=session,
        )
    assert len(page["items"]) == 1
    assert page["items"][0]["componentId"] == first.id
    assert page["items"][0]["status"] == "failed"
