import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.domain.workflow import ExecutionError, NodeResult, NodeType
from app.infrastructure.database import (
    NodeRun,
    Run,
    RunEvent,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
)


def workflow_definition(
    *,
    max_parallel: int = 2,
    max_duration: int = 60,
    on_error: str = "fail_run",
    max_attempts: int = 2,
) -> dict[str, Any]:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "worker-correctness"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
                "additionalProperties": False,
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "work",
                    "type": "transform",
                    "displayName": "慢任务",
                    "input": {"value": "${input.value}"},
                    "config": {"operation": "passthrough"},
                    "retry": {"maxAttempts": max_attempts, "backoffSeconds": 0},
                    "timeoutSeconds": 5,
                    "onError": on_error,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "result": (
                            "${work.error.code}"
                            if on_error == "continue"
                            else "${work.output.value}"
                        )
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "work"},
                {"source": "work", "target": "output"},
            ],
            "policies": {
                "maxParallelNodes": max_parallel,
                "maxDurationSeconds": max_duration,
            },
        },
    }


def diamond_workflow(max_parallel: int = 2) -> dict[str, Any]:
    definition = workflow_definition(max_parallel=max_parallel)
    definition["spec"]["nodes"] = [
        {"id": "input", "type": "input", "displayName": "输入"},
        {
            "id": "left",
            "type": "transform",
            "displayName": "左分支",
            "input": {"value": "${input.value}"},
        },
        {
            "id": "right",
            "type": "transform",
            "displayName": "右分支",
            "input": {"value": "${input.value}"},
        },
        {
            "id": "output",
            "type": "output",
            "displayName": "输出",
            "input": {"result": "${left.output.value}-${right.output.value}"},
        },
    ]
    definition["spec"]["edges"] = [
        {"source": "input", "target": "left"},
        {"source": "input", "target": "right"},
        {"source": "left", "target": "output"},
        {"source": "right", "target": "output"},
    ]
    return definition


async def create_run(definition: dict[str, Any]) -> str:
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="correctness",
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
        run = await Orchestrator().create_run(session, version, {"value": "ok"})
        return run.id


class SlowExecutor:
    node_type = NodeType.TRANSFORM

    def __init__(self, delay: float = 0.4) -> None:
        self.delay = delay
        self.calls = 0
        self.cancelled = 0

    async def execute(self, context: Any, node: Any, inputs: dict[str, Any]) -> NodeResult:
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
            context.cancellation.check()
            return NodeResult(status="succeeded", output=inputs)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FailingExecutor:
    node_type = NodeType.TRANSFORM

    async def execute(self, context: Any, node: Any, inputs: dict[str, Any]) -> NodeResult:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="EXPECTED_FAILURE", message="预期失败"),
        )


async def test_heartbeat_prevents_second_worker_claim() -> None:
    run_id = await create_run(workflow_definition())
    first = WorkerService("worker-1", lease_seconds=1)
    second = WorkerService("worker-2", lease_seconds=1)
    await first.run_once()
    slow = SlowExecutor(delay=0.55)
    first.registry._executors[NodeType.TRANSFORM] = slow
    second.registry._executors[NodeType.TRANSFORM] = slow

    running = asyncio.create_task(first.run_once())
    await asyncio.sleep(0.4)
    assert await second.run_once() is False
    assert await running is True
    assert slow.calls == 1

    async with SessionLocal() as session:
        node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "work")
        )
        assert node is not None
        assert node.status == "succeeded"


async def test_stale_worker_result_cannot_overwrite_takeover() -> None:
    run_id = await create_run(workflow_definition())
    worker = WorkerService("worker-old", lease_seconds=1)
    await worker.run_once()
    async with SessionLocal() as session, session.begin():
        claimed = await worker.claim(session)
    assert claimed is not None
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(NodeRun)
            .where(NodeRun.id == claimed.node_run_id)
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
    recovering = WorkerService("worker-new", lease_seconds=1)
    async with SessionLocal() as session, session.begin():
        assert await recovering.recover_expired(session) == 1

    await worker._commit_result(
        claimed,
        NodeResult(status="succeeded", output={"value": "stale"}),
    )

    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == "work")
                .order_by(NodeRun.attempt)
            )
        ).all()
        assert [(item.attempt, item.status) for item in rows] == [
            (1, "lease_expired"),
            (2, "ready"),
        ]
        stale = await session.scalar(
            select(func.count())
            .select_from(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.type == "node.stale_result_ignored")
        )
        assert stale == 1


async def test_running_cancel_stops_executor_before_terminal_state() -> None:
    run_id = await create_run(workflow_definition())
    worker = WorkerService("worker-cancel", lease_seconds=1)
    await worker.run_once()
    slow = SlowExecutor(delay=5)
    worker.registry._executors[NodeType.TRANSFORM] = slow
    running = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.15)

    async with SessionLocal() as session, session.begin():
        await Orchestrator().request_cancellation(session, run_id)

    await asyncio.wait_for(running, timeout=2)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "work")
        )
        assert run is not None and run.status == "cancelled"
        assert node is not None and node.status == "cancelled"
        assert slow.cancelled == 1


async def test_on_error_continue_requires_explicit_error_mapping() -> None:
    run_id = await create_run(workflow_definition(on_error="continue"))
    worker = WorkerService("worker-continue")
    await worker.run_once()
    worker.registry._executors[NodeType.TRANSFORM] = FailingExecutor()
    await worker.run_once()
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.output_json == {"result": "EXPECTED_FAILURE"}


async def test_non_idempotent_timeout_never_retries_automatically() -> None:
    definition = workflow_definition(max_attempts=1)
    work = definition["spec"]["nodes"][1]
    work["sideEffect"] = True
    work["idempotent"] = False
    work["timeoutSeconds"] = 1
    run_id = await create_run(definition)
    worker = WorkerService("worker-side-effect")
    await worker.run_once()
    worker.registry._executors[NodeType.TRANSFORM] = SlowExecutor(delay=2)

    await worker.run_once()

    async with SessionLocal() as session:
        attempts = (
            await session.scalars(
                select(NodeRun).where(
                    NodeRun.run_id == run_id,
                    NodeRun.node_id == "work",
                )
            )
        ).all()
        run = await session.get(Run, run_id)
        assert len(attempts) == 1
        assert attempts[0].status == "waiting_user"
        assert attempts[0].error_json["code"] == "UNKNOWN_OUTCOME"
        assert run is not None and run.status == "waiting_approval"


async def test_run_duration_requests_cancellation() -> None:
    run_id = await create_run(workflow_definition(max_duration=1))
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        assert run is not None
        run.started_at = utcnow() - timedelta(seconds=2)
        await Orchestrator().advance_run(session, run_id)

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert run.cancellation_requested_at is not None


async def test_max_parallel_nodes_limits_claims() -> None:
    await create_run(diamond_workflow(max_parallel=1))
    first = WorkerService("worker-limit-1")
    second = WorkerService("worker-limit-2")
    await first.run_once()

    async with SessionLocal() as session, session.begin():
        assert await first.claim(session) is not None
    async with SessionLocal() as session, session.begin():
        assert await second.claim(session) is None


async def test_concurrent_diamond_advance_creates_single_output_and_unique_events() -> None:
    run_id = await create_run(diamond_workflow())
    worker = WorkerService("worker-diamond")
    await worker.run_once()
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id.in_(["left", "right"]))
            .values(status="succeeded", output_json={"value": "ok"}, finished_at=utcnow())
        )

    async def advance() -> None:
        async with SessionLocal() as session, session.begin():
            await Orchestrator().advance_run(session, run_id)

    await asyncio.gather(advance(), advance())

    async with SessionLocal() as session:
        outputs = await session.scalar(
            select(func.count())
            .select_from(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == "output")
        )
        seqs = (
            await session.scalars(
                select(RunEvent.seq).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
            )
        ).all()
        assert outputs == 1
        assert list(seqs) == list(range(1, len(seqs) + 1))
