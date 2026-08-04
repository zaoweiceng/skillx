import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.config import get_settings
from app.infrastructure.database import (
    NodeRun,
    Run,
    RunEvent,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


def agent_workflow(*, allowed: list[str], prompt: str = "完成任务") -> dict[str, Any]:
    output_schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "traex-demo"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "agent",
                    "type": "agent",
                    "displayName": "TraeX Agent",
                    "input": {"task": "${input.task}"},
                    "config": {
                        "outputSchema": output_schema,
                        "maxSteps": 4,
                        "maxTotalTokens": 500,
                        "allowedCapabilities": allowed,
                        "prompt": prompt,
                    },
                    "timeoutSeconds": 5,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"reply": "${agent.output.reply}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "agent"},
                {"source": "agent", "target": "output"},
            ],
        },
    }


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(settings, "traex_allowed_executables", [Path(sys.executable).name])


async def _run(definition: dict[str, Any]) -> str:
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()), name="traex", draft_revision=1, draft_json=definition
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
        run = await Orchestrator().create_run(session, version, {"task": "分类反馈"})
        return run.id


async def _drive(run_id: str) -> Run:
    worker = WorkerService("traex-worker")
    for _ in range(4):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run


async def test_traex_agent_records_plan_tool_and_output(traex_stub: None) -> None:
    run_id = await _run(
        agent_workflow(allowed=["search.web"], prompt="完成任务 [[MODE:ok]][[REPLY:分类完成]]")
    )
    run = await _drive(run_id)
    assert run.status == "succeeded"
    assert run.output_json == {"reply": "分类完成"}
    async with SessionLocal() as session:
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
        assert {"agent.plan", "tool.call.started", "agent.completed"} <= event_types
        node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "agent")
        )
        assert node is not None


async def test_traex_unauthorized_tool_fails_run(traex_stub: None) -> None:
    run_id = await _run(
        agent_workflow(allowed=["search.web"], prompt="完成任务 [[MODE:unauthorized_tool]]")
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    async with SessionLocal() as session:
        node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "agent")
        )
        assert node is not None and node.status == "failed"
        assert node.error_json["code"] == "PERMISSION_DENIED"


async def test_traex_no_output_fails(traex_stub: None) -> None:
    run_id = await _run(
        agent_workflow(allowed=["search.web"], prompt="完成任务 [[MODE:no_output]]")
    )
    run = await _drive(run_id)
    assert run.status == "failed"


async def test_traex_bad_schema_fails(traex_stub: None) -> None:
    run_id = await _run(
        agent_workflow(allowed=["search.web"], prompt="完成任务 [[MODE:bad_schema]]")
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    async with SessionLocal() as session:
        node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "agent")
        )
        assert node is not None and node.error_json["code"] == "CONTRACT_VIOLATION"
