from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes import create_message, workspace_directories
from app.api.schemas import MessageCreate
from app.application.local_execution import execute_local
from app.application.product_history import turn_view, usage_summary
from app.application.tasks import finish_task
from app.infrastructure.database import Conversation, ConversationTurn, SessionLocal, utcnow
from app.infrastructure.models.tasks import Task


async def test_finished_task_time_is_frozen_and_tokens_are_not_zero():
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="metrics")
        session.add(conversation)
        await session.flush()
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
            accepted_at=utcnow() - timedelta(seconds=12),
        )
        session.add(turn)
        await session.flush()
        task = Task(
            id=str(uuid4()),
            conversation_id=conversation.id,
            turn_id=turn.id,
            goal="test",
            status="deciding",
            total_tokens=1234,
        )
        session.add(task)
        await session.flush()
        await finish_task(session, task, "succeeded", "done")
        first = await turn_view(session, turn)
        assert 12000 <= first["elapsedMs"] < 14000
        assert first["usage"]["totalTokens"] == 1234
        assert first["usage"]["state"] == "partial"
        # Old records didn't persist elapsed_ms: completed_at still freezes the clock.
        turn.elapsed_ms = None
        assert (await turn_view(session, turn))["elapsedMs"] == first["elapsedMs"]
        await finish_task(session, task, "succeeded", "done")
        assert (await turn_view(session, turn))["elapsedMs"] == first["elapsedMs"]
        task.total_tokens = 0
        assert (await usage_summary(session, turn.id))["state"] == "unavailable"


async def test_directory_selection_and_message_validation(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "file.txt").write_text("file")
    listing = await workspace_directories(str(tmp_path))
    assert listing["directories"] == [{"name": "project", "path": str(tmp_path / "project")}]
    async with SessionLocal() as session:
        conversation = Conversation(id=str(uuid4()), title="directory")
        session.add(conversation)
        await session.commit()
        with pytest.raises(HTTPException):
            await create_message(
                conversation.id,
                MessageCreate(content="test", workingDirectory=str(tmp_path / "missing")),
                session,
            )
        result = await create_message(
            conversation.id, MessageCreate(content="test", workingDirectory=str(tmp_path)), session
        )
        task = await session.get(Task, result["taskId"])
        assert task.state_json["workingDirectory"] == str(tmp_path)


async def test_code_executes_in_selected_directory_without_moving_evidence(tmp_path, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "task_local_execution_enabled", True)
    monkeypatch.setattr(settings, "artifact_root", tmp_path / "artifacts")
    selected = tmp_path / "project"
    selected.mkdir()
    (selected / "input.txt").write_text("selected directory")

    async def cancelled():
        return False

    result = await execute_local(
        "directory-test",
        str(uuid4()),
        "code",
        {
            "language": "python",
            "source": "from pathlib import Path\nprint(Path('input.txt').read_text())",
        },
        cancelled,
        working_directory=str(selected),
    )
    assert result["exitCode"] == 0
    assert result["stdout"].strip() == "selected directory"
    assert not (selected / ".executions").exists()


async def test_workflow_metric_fallback_does_not_double_count_ledger():
    from app.application.product_history import record_model_usage
    from app.infrastructure.database import Run, RunEvent, Workflow, WorkflowVersion
    from app.infrastructure.models.tasks import Invocation

    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="nested usage")
        workflow = Workflow(id=str(uuid4()), name="test", draft_json={})
        session.add_all([conversation, workflow])
        await session.flush()
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="succeeded",
            accepted_at=utcnow(),
            completed_at=utcnow(),
        )
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json={},
            digest="test",
            validation_json={},
        )
        session.add_all([turn, version])
        await session.flush()
        task = Task(
            id=str(uuid4()), turn_id=turn.id, goal="usage", status="succeeded", total_tokens=100
        )
        run = Run(
            id=str(uuid4()), workflow_version_id=version.id, input_json={}, status="succeeded"
        )
        session.add_all([task, run])
        await session.flush()
        session.add(
            Invocation(
                id=str(uuid4()),
                task_id=task.id,
                scope=task.id,
                call_key="workflow",
                kind="workflow",
                status="succeeded",
                request_json={},
                run_id=run.id,
            )
        )
        session.add(
            RunEvent(
                id=str(uuid4()),
                run_id=run.id,
                seq=1,
                type="node.completed",
                payload_json={"metrics": {"totalTokens": 40}},
            )
        )
        await session.flush()
        assert (await usage_summary(session, turn.id))["totalTokens"] == 140
        # When the run is already accounted for in Task, its ledger prevents duplication.
        await record_model_usage(
            session,
            call_id="workflow-call",
            stage="workflow_agent",
            run_id=run.id,
            total_tokens=40,
            usage_complete=True,
        )
        task.total_tokens = 140
        assert (await usage_summary(session, turn.id))["totalTokens"] == 140


async def test_conversation_directory_survives_messages_reload_and_explicit_clear(tmp_path):
    from app.api.routes import get_conversation, update_conversation_directory
    from app.api.schemas import ConversationDirectoryUpdate

    async with SessionLocal() as session:
        conversation = Conversation(id=str(uuid4()), title="persistent directory")
        session.add(conversation)
        await session.commit()
        first = await create_message(
            conversation.id, MessageCreate(content="first", workingDirectory=str(tmp_path)), session
        )
        second = await create_message(conversation.id, MessageCreate(content="second"), session)
        for response in [first, second]:
            assert (await session.get(Task, response["taskId"])).state_json[
                "workingDirectory"
            ] == str(tmp_path)
    async with SessionLocal() as session:
        assert (await get_conversation(conversation.id, session))["workingDirectory"] == str(
            tmp_path
        )
        await update_conversation_directory(
            conversation.id, ConversationDirectoryUpdate(workingDirectory=None), session
        )
        third = await create_message(conversation.id, MessageCreate(content="third"), session)
        assert (await session.get(Task, third["taskId"])).state_json["workingDirectory"] is None
        # Clearing changes future turns, never an accepted task's execution snapshot.
        assert (await session.get(Task, first["taskId"])).state_json["workingDirectory"] == str(
            tmp_path
        )


async def test_file_tool_uses_conversation_task_directory(tmp_path):
    from app.application.invocations import InvocationDriver, enqueue_invocation
    from app.infrastructure.models.tasks import Invocation

    (tmp_path / "note.txt").write_text("selected workspace")
    async with SessionLocal() as session, session.begin():
        task = Task(
            id=str(uuid4()),
            goal="read",
            status="waiting_execution",
            state_json={"workingDirectory": str(tmp_path)},
        )
        session.add(task)
        await session.flush()
        call = await enqueue_invocation(
            session,
            task=task,
            kind="tool",
            key="read-note",
            arguments={"capabilityRef": "read.local", "input": {"path": "note.txt"}},
        )
        call_id = call.id
    assert await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        call = await session.get(Invocation, call_id)
        assert call.status == "succeeded", call.error_json
        assert "selected workspace" in str(call.output_json)


async def test_running_task_exposes_committed_model_usage_without_waiting_for_completion():
    from app.infrastructure.models.tasks import TaskEvent

    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="live usage")
        session.add(conversation)
        await session.flush()
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
        )
        session.add(turn)
        await session.flush()
        task = Task(
            id=str(uuid4()),
            conversation_id=conversation.id,
            turn_id=turn.id,
            goal="live",
            status="deciding",
            total_tokens=0,
        )
        session.add(task)
        await session.flush()
        for kind, payload in [
            ("model.request.dispatched", {"requestId": "first", "model": "pi-model"}),
            (
                "agent.step.completed",
                {
                    "requestId": "first",
                    "inputTokens": 1000,
                    "outputTokens": 234,
                    "usageComplete": True,
                },
            ),
            ("model.request.dispatched", {"requestId": "second", "model": "pi-model"}),
        ]:
            session.add(
                TaskEvent(
                    task_id=task.id,
                    type="agent.audit",
                    payload_json={"type": kind, "payload": payload},
                )
            )
        await session.flush()
        usage = await usage_summary(session, turn.id)
        assert usage["state"] == "pending"
        assert usage["totalTokens"] == 1234
        assert usage["stages"][1]["totalTokens"] is None
        task.total_tokens = 1234
        assert (await usage_summary(session, turn.id))["totalTokens"] == 1234
        task.status = "cancelled"
        usage = await usage_summary(session, turn.id)
        assert usage["state"] == "partial" and usage["totalTokens"] == 1234


async def test_first_model_request_without_usage_remains_unknown():
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="waiting usage")
        session.add(conversation)
        await session.flush()
        turn = ConversationTurn(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_mode_snapshot="enabled",
            status="processing",
        )
        session.add(turn)
        await session.flush()
        session.add(
            Task(
                id=str(uuid4()),
                conversation_id=conversation.id,
                turn_id=turn.id,
                goal="waiting",
                status="deciding",
                total_tokens=0,
            )
        )
        await session.flush()
        assert (await usage_summary(session, turn.id))["totalTokens"] is None
