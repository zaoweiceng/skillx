import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.mcp import discover_tools, register_server, validate_mcp_nodes
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import Run, RunEvent, SessionLocal, Workflow, WorkflowVersion


def mcp_workflow(snapshot_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "local-weather"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "condition": {"type": "string"},
                    "temperature": {"type": "number"},
                },
                "required": ["city", "condition", "temperature"],
                "additionalProperties": False,
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "weather",
                    "type": "mcp",
                    "displayName": "查询天气",
                    "input": {"city": "${input.city}"},
                    "config": {"mcpToolSnapshotId": snapshot_id},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "city": "${weather.output.city}",
                        "condition": "${weather.output.condition}",
                        "temperature": "${weather.output.temperature}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "weather"},
                {"source": "weather", "target": "output"},
            ],
        },
    }


async def test_stdio_mcp_discover_call_and_zero_llm_workflow() -> None:
    server_file = Path(__file__).resolve().parents[2] / "examples" / "local_weather_mcp.py"
    async with SessionLocal() as session, session.begin():
        _, server_version = await register_server(
            session,
            "local-weather",
            "stdio",
            {
                "command": [sys.executable, str(server_file)],
                "timeoutSeconds": 5,
            },
        )
        snapshots = await discover_tools(session, server_version.id)
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        definition = mcp_workflow(snapshot.id)
        compiled = WorkflowCompiler().compile(definition)
        locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
        assert locks[0]["digest"] == snapshot.digest
        workflow = Workflow(
            id=str(uuid4()),
            name="local-weather",
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
            validation_json={"executionClass": "zero_llm", "mcpLocks": locks},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"city": "成都"})
        run_id = run.id

    worker = WorkerService("mcp-worker")
    assert await worker.run_once()
    assert await worker.run_once()
    assert await worker.run_once()

    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.output_json is not None
        assert completed.output_json["city"] == "成都"
        mcp_event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.type == "mcp.call.completed",
            )
        )
        assert mcp_event is not None
        assert mcp_event.payload_json["toolDigest"] == snapshot.digest
