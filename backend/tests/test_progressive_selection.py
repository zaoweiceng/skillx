from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.api.routes import MessageCreate, _select_workflow_candidate, create_message
from app.application.compiler import WorkflowCompiler
from app.application.workflow_catalog import build_manifest, create_disclosure_session
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    PendingSelection,
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
                        "operation": "format",
                        "template": "你好，{name}",
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"message": "${format.output.message}"},
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


@pytest.mark.parametrize(
    ("requested", "expected", "levels"),
    [
        ("run_full", "run_full", {"L0", "L1"}),
        ("run_fragment", "run_fragment", {"L0", "L1"}),
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
        selection, timeline = await _select_workflow_candidate(
            session,
            f"渐进问候 [[SELECTION:{requested}]]",
            disclosure.id,
        )
    assert selection.action == expected
    assert {item["level"] for item in timeline} == levels
