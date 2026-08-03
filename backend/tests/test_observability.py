from typing import Any
from uuid import uuid4

from app.api.routes import get_component, get_run
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import (
    Component,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)
from tests.fixtures import deterministic_workflow


async def _seed_component_and_run() -> tuple[str, str]:
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()), name="obs-wf", draft_revision=1, draft_json=definition
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={"executionClass": "zero_llm", "capabilityLocks": [], "mcpLocks": []},
        )
        session.add(version)
        await session.flush()
        component = Component(
            id=str(uuid4()),
            name="可观测问候",
            workflow_id=workflow.id,
            default_version_id=version.id,
            execution_class="zero_llm",
        )
        session.add(component)
        run = await Orchestrator().create_run(session, version, {"name": "Alice"}, component.id)
        return component.id, run.id


async def test_run_detail_exposes_node_timing_and_dependency_versions() -> None:
    _, run_id = await _seed_component_and_run()
    worker = WorkerService("obs-worker")
    for _ in range(3):
        await worker.run_once()

    async with SessionLocal() as session:
        detail: dict[str, Any] = await get_run(run_id, session)

    assert detail["status"] == "succeeded"
    assert detail["dependencyVersions"]["executionClass"] == "zero_llm"
    assert detail["dependencyVersions"]["workflowDigest"]
    # 每个节点都带 durationMs 与 metrics 字段（可为空但存在）。
    for node in detail["nodes"]:
        assert "durationMs" in node
        assert "metrics" in node
    succeeded = [node for node in detail["nodes"] if node["status"] == "succeeded"]
    assert succeeded and all(node["durationMs"] is not None for node in succeeded)


async def test_component_detail_exposes_business_and_technical_views() -> None:
    component_id, _ = await _seed_component_and_run()
    async with SessionLocal() as session:
        detail: dict[str, Any] = await get_component(component_id, session)

    assert "businessView" in detail and "technicalView" in detail
    assert detail["technicalView"]["nodeCount"] >= 2
    assert detail["businessView"]["recentRunCount"] >= 0
