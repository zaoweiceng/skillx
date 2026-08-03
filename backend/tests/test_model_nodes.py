from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import Run, RunEvent, SessionLocal, Workflow, WorkflowVersion


def model_workflow(node_type: str = "llm") -> dict[str, Any]:
    output_schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    }
    config: dict[str, Any] = {
        "outputSchema": output_schema,
        "fakeOutput": {"category": "feedback"},
        "prompt": "分类：{input}",
    }
    if node_type == "llm":
        config["modelProfile"] = "fake"
    else:
        config.update(
            agentProfile="fake",
            maxSteps=3,
            maxTotalTokens=100,
            allowedCapabilities=[],
        )
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": f"{node_type}-demo"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "semantic",
                    "type": node_type,
                    "displayName": "语义节点",
                    "input": {"text": "${input.text}"},
                    "config": config,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"category": "${semantic.output.category}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "semantic"},
                {"source": "semantic", "target": "output"},
            ],
        },
    }


async def execute_workflow(definition: dict[str, Any]) -> tuple[str, Run]:
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="model-demo",
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
        run = await Orchestrator().create_run(session, version, {"text": "产品很好"})
        run_id = run.id
    worker = WorkerService("model-worker")
    for _ in range(3):
        assert await worker.run_once()
    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        return run_id, completed


async def test_hybrid_workflow_only_llm_node_reports_tokens() -> None:
    run_id, run = await execute_workflow(model_workflow("llm"))

    assert run.status == "succeeded"
    assert run.output_json == {"category": "feedback"}
    async with SessionLocal() as session:
        events = (
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run_id,
                    RunEvent.type == "llm.call.completed",
                )
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload_json["inputTokens"] > 0


async def test_fake_agent_emits_auditable_events() -> None:
    run_id, run = await execute_workflow(model_workflow("agent"))

    assert run.status == "succeeded"
    async with SessionLocal() as session:
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
        assert {"agent.plan", "agent.step.completed", "agent.completed"} <= event_types


def test_agent_without_limits_is_rejected() -> None:
    definition = model_workflow("agent")
    definition["spec"]["nodes"][1]["config"].pop("maxSteps")

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)

    assert "AGENT_LIMITS_REQUIRED" in {item.code for item in error.value.diagnostics}
