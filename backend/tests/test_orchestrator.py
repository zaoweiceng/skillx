from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import Approval, Run, SessionLocal, Workflow, WorkflowVersion
from tests.fixtures import deterministic_workflow


async def create_version(with_approval: bool = False) -> WorkflowVersion:
    definition = deterministic_workflow(with_approval)
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session:
        workflow = Workflow(
            id=str(uuid4()),
            name="greeting",
            draft_json=definition,
            draft_revision=1,
        )
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add_all([workflow, version])
        await session.commit()
        return version


async def test_deterministic_workflow_completes_without_model() -> None:
    version = await create_version()
    async with SessionLocal() as session:
        async with session.begin():
            run = await Orchestrator().create_run(session, version, {"name": "成都"})
        run_id = run.id

    worker = WorkerService("test-worker")
    for _ in range(3):
        assert await worker.run_once()

    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.output_json == {"message": "你好，成都"}


async def test_approval_pauses_without_holding_worker() -> None:
    version = await create_version(with_approval=True)
    async with SessionLocal() as session:
        async with session.begin():
            run = await Orchestrator().create_run(session, version, {"name": "Alice"})
        run_id = run.id

    worker = WorkerService("test-worker")
    assert await worker.run_once()
    assert await worker.run_once()
    assert await worker.run_once()

    async with SessionLocal() as session:
        paused = await session.get(Run, run_id)
        approval = await session.scalar(select(Approval).where(Approval.run_id == run_id))
        assert paused is not None
        assert paused.status == "waiting_approval"
        assert approval is not None
        assert approval.status == "pending"
        assert await worker.run_once() is False
