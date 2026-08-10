from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.routes import (
    MessageCreate,
    _select_workflow_candidate,
    _verify_binding_sources,
    create_message,
)
from app.application.agent_run import AgentRunWorker
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import WorkerService
from app.application.plans import WorkflowSelection
from app.application.workflow_catalog import build_manifest, create_disclosure_session
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    Message,
    PendingSelection,
    PlanSnapshot,
    Run,
    SessionLocal,
    Workflow,
    WorkflowDisclosureCall,
    WorkflowVersion,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


def greeting_definition() -> dict:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {
            "name": "progressive-greeting",
            "capability": {
                "summary": "根据姓名生成渐进问候",
                "useWhen": ["需要问候某人"],
                "tags": ["问候", "greeting"],
            },
            "callableFragments": [
                {
                    "name": "format-only",
                    "summary": "只生成问候",
                    "nodeIds": ["format"],
                }
            ],
        },
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "format",
                    "type": "transform",
                    "displayName": "生成问候",
                    "input": {"name": "${input.name}"},
                    "config": {
                        "operation": "template",
                        "template": "你好，{name}",
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"message": "${format.output.text}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "format"},
                {"source": "format", "target": "output"},
            ],
        },
    }


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_provider_type", "fixture")
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(
        settings, "traex_allowed_executables", [Path(sys.executable).name]
    )


async def _seed() -> str:
    definition = greeting_definition()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="渐进问候",
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
        await build_manifest(session, workflow, version)
        conversation = Conversation(id=str(uuid4()), title="渐进选择")
        session.add(conversation)
        await session.flush()
        return conversation.id


async def test_ask_user_resumes_original_selection_without_research(
    traex_stub: None,
) -> None:
    conversation_id = await _seed()
    async with SessionLocal() as session:
        first = await create_message(
            conversation_id,
            MessageCreate(content="请运行渐进问候"),
            session,
        )
    assistant = first["assistant"]
    assert assistant["state"] == "ask_user"
    assert assistant["required"] == ["name"]
    assert assistant["refs"][0].startswith("workflow:")

    async with SessionLocal() as session:
        searches_before = await session.scalar(
            select(func.count())
            .select_from(WorkflowDisclosureCall)
            .where(WorkflowDisclosureCall.operation == "search")
        )
        pending = await session.scalar(select(PendingSelection))
        assert pending is not None and pending.original_task == "请运行渐进问候"

    async with SessionLocal() as session:
        second = await create_message(
            conversation_id,
            MessageCreate(content="Alice"),
            session,
        )
    assert second["assistant"]["state"] == "workflow_run"
    assert second["assistant"]["resumedPendingSelection"] is True

    async with SessionLocal() as session:
        searches_after = await session.scalar(
            select(func.count())
            .select_from(WorkflowDisclosureCall)
            .where(WorkflowDisclosureCall.operation == "search")
        )
        run = await session.get(Run, second["assistant"]["runId"])
        pending = await session.scalar(select(PendingSelection))
    assert searches_after == searches_before
    assert run is not None and run.input_json == {"name": "Alice"}
    assert pending is not None and pending.status == "resumed"


async def test_binding_sources_are_verified_against_server_evidence() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="source")
        other = Conversation(id=str(uuid4()), title="other")
        message = Message(
            id=str(uuid4()),
            conversation_id=conversation.id,
            role="user",
            content_json={"type": "text", "text": "上海天气如何"},
        )
        foreign = Message(
            id=str(uuid4()),
            conversation_id=other.id,
            role="user",
            content_json={"type": "text", "text": "上海"},
        )
        session.add_all([conversation, other, message, foreign])

    def selection(source: dict | None) -> WorkflowSelection:
        return WorkflowSelection(
            action="run_full",
            refs=["workflow:test@1#digest"],
            bindings={"city": "上海"},
            bindingSources=[source] if source else [],
            sessionId=str(uuid4()),
        )

    valid_source = {
        "field": "city",
        "source": "user_message",
        "messageId": message.id,
        "quote": "上海",
        "confidence": 0.99,
    }
    async with SessionLocal() as session:
        verified, invalid = await _verify_binding_sources(
            session,
            selection(valid_source),
            conversation_id=conversation.id,
            current_message_id=message.id,
            tool_evidence={},
        )
        assert invalid == []
        assert verified.binding_sources[0].server_verified is True
        assert verified.binding_sources[0].normalization_type == "exact"

        with pytest.raises(ValueError, match="bindingSource"):
            selection(None)
        with pytest.raises(ValueError, match="bindingSource"):
            selection({**valid_source, "field": "other"})

        for forged in (
            {**valid_source, "messageId": str(uuid4())},
            {**valid_source, "messageId": foreign.id},
            {**valid_source, "quote": "北京"},
        ):
            _verified, invalid = await _verify_binding_sources(
                session,
                selection(forged),
                conversation_id=conversation.id,
                current_message_id=message.id,
                tool_evidence={},
            )
            assert "city" in invalid

        tool_selection = selection(
            {
                "field": "city",
                "source": "tool_result",
                "callId": "call-1",
                "path": "output.city",
                "quote": "上海",
                "confidence": 1,
            }
        )
        verified, invalid = await _verify_binding_sources(
            session,
            tool_selection,
            conversation_id=conversation.id,
            current_message_id=message.id,
            tool_evidence={"call-1": {"city": "上海"}},
        )
        assert invalid == []
        assert verified.binding_sources[0].normalization_type == "tool_result_path"


async def test_invalid_agent_binding_source_becomes_ask_user(
    traex_stub: None,
) -> None:
    conversation_id = await _seed()
    async with SessionLocal() as session:
        response = await create_message(
            conversation_id,
            MessageCreate(
                content="渐进问候 [[SELECTION:run_full]][[SOURCE:forged]]"
            ),
            session,
        )

    assert response["assistant"]["state"] == "ask_user"
    assert response["assistant"]["required"] == ["name"]
    async with SessionLocal() as session:
        assert await session.scalar(select(Run)) is None
        pending = await session.scalar(select(PendingSelection))
    assert pending is not None
    assert pending.bindings_json == {}


@pytest.mark.parametrize(
    ("requested", "expected", "levels"),
    [
        ("run_full", "run_full", {"L0", "L1"}),
        ("run_fragment", "run_fragment", {"L0", "L1", "L2"}),
        ("compose", "compose", {"L0", "L1", "L2"}),
        ("ask_user", "ask_user", {"L0", "L1"}),
        ("continue_agent", "continue_agent", {"L0"}),
    ],
)
async def test_selection_five_actions_come_from_progressive_agent_loop(
    traex_stub: None,
    requested: str,
    expected: str,
    levels: set[str],
) -> None:
    conversation_id = await _seed()
    async with SessionLocal() as session, session.begin():
        disclosure = await create_disclosure_session(
            session, conversation_id=conversation_id
        )
        selection, timeline, _tool_evidence = await _select_workflow_candidate(
            session,
            f"渐进问候 [[SELECTION:{requested}]]",
            disclosure.id,
        )
    assert selection.action == expected
    assert {item["level"] for item in timeline} == levels

    async with SessionLocal() as session:
        snapshots_before = await session.scalar(
            select(func.count()).select_from(PlanSnapshot)
        )
        response = await create_message(
            conversation_id,
            MessageCreate(content=f"渐进问候 [[SELECTION:{requested}]]"),
            session,
        )
    assistant = response["assistant"]
    if requested == "ask_user":
        assert assistant["state"] == "ask_user"
        return
    if requested == "continue_agent":
        assert assistant["state"] == "queued"
        run_id = assistant["conversationRunId"]
        assert await AgentRunWorker("selection-continuation").poll_once()
        async with SessionLocal() as session:
            run = await session.get(ConversationRun, run_id)
        assert run is not None and run.status == "succeeded"
        context = run.input_json["disclosureContext"]
        assert context[0]["level"] == "L0"
        assert context[0]["result"]["candidates"][0]["workflowRef"]
        assert context[0]["resultDigest"].startswith("sha256:")
        assert "inputSchema" not in str(context)
        assert run.input_json["remainingBudget"]["disclosureCallsUsed"] == 1
        return

    assert assistant["state"] == "workflow_run"
    worker = WorkerService(f"selection-{requested}")
    for _ in range(6):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, assistant["runId"])
        snapshots_after = await session.scalar(
            select(func.count()).select_from(PlanSnapshot)
        )
    assert run is not None and run.status == "succeeded", run.error_json if run else None
    assert run.output_json == {"message": "你好，渐进问候"}
    expected_snapshots = 1 if requested in {"run_fragment", "compose"} else 0
    assert snapshots_after - snapshots_before == expected_snapshots
