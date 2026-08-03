from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.capabilities import CapabilityResolutionError, resolve_skillx_node
from app.application.compiler import digest_json
from app.application.expressions import ExpressionError, resolve_mapping
from app.application.mcp import MCPConfigurationError, MCPLockDriftError, resolve_mcp_node
from app.application.retries import auto_retry_allowed
from app.application.security import event_payload
from app.domain.workflow import NodeResult, NodeType, WorkflowDefinition, WorkflowNode
from app.infrastructure.database import (
    Approval,
    Artifact,
    NodeRun,
    Run,
    RunEvent,
    SessionLocal,
    WorkflowVersion,
    utcnow,
)
from app.runtime.executors import (
    CancellationToken,
    ExecutionCancelledError,
    ExecutorRegistry,
    RunContext,
    clear_staging,
    promote_staged_artifact,
)

FINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}
TERMINAL_NODE_STATES = {
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    "lease_expired",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def append_event(
    session: AsyncSession,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    node_run_id: str | None = None,
) -> RunEvent:
    seq = await session.scalar(
        update(Run)
        .where(Run.id == run_id)
        .values(next_event_seq=Run.next_event_seq + 1)
        .returning(Run.next_event_seq)
    )
    if seq is None:
        raise LookupError("run not found")
    event = RunEvent(
        id=str(uuid4()),
        run_id=run_id,
        node_run_id=node_run_id,
        seq=seq,
        type=event_type,
        payload_json=event_payload(payload),
    )
    session.add(event)
    await session.flush()
    return event


class Orchestrator:
    async def create_run(
        self,
        session: AsyncSession,
        workflow_version: WorkflowVersion,
        inputs: dict[str, Any],
        component_id: str | None = None,
        owner_id: str = "local-user",
    ) -> Run:
        definition = WorkflowDefinition.model_validate(workflow_version.definition_json)
        errors = list(Draft202012Validator(definition.spec.input_schema).iter_errors(inputs))
        if errors:
            raise ValueError(errors[0].message)
        run = Run(
            id=str(uuid4()),
            workflow_version_id=workflow_version.id,
            component_id=component_id,
            owner_id=owner_id,
            status="queued",
            input_json=inputs,
        )
        session.add(run)
        await session.flush()
        await append_event(
            session,
            run.id,
            "run.created",
            {"runId": run.id, "workflowVersionId": workflow_version.id},
        )
        await self.advance_run(session, run.id)
        return run

    async def request_cancellation(self, session: AsyncSession, run_id: str) -> Run:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError("run not found")
        if run.status not in FINAL_RUN_STATES and run.status != "cancelling":
            run.status = "cancelling"
            run.cancellation_requested_at = utcnow()
            await append_event(session, run.id, "run.cancellation_requested", {"runId": run.id})
        await self.advance_run(session, run.id)
        return run

    async def advance_run(self, session: AsyncSession, run_id: str) -> Run:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError("run not found")
        if run.status in FINAL_RUN_STATES:
            return run
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:
            raise LookupError("workflow version not found")
        definition = WorkflowDefinition.model_validate(version.definition_json)
        nodes = {node.id: node for node in definition.spec.nodes}

        if self._duration_exceeded(run, definition):
            if run.status != "cancelling":
                run.status = "cancelling"
                run.cancellation_requested_at = utcnow()
                await append_event(
                    session,
                    run.id,
                    "run.timeout_requested",
                    {
                        "runId": run.id,
                        "maxDurationSeconds": definition.spec.policies.max_duration_seconds,
                    },
                )

        if run.cancellation_requested_at:
            await session.execute(
                update(NodeRun)
                .where(NodeRun.run_id == run.id)
                .where(NodeRun.status.in_(["pending", "ready", "retry_waiting"]))
                .values(
                    status="cancelled",
                    error_json={
                        "code": "CANCELLED",
                        "message": "运行取消，节点未执行",
                        "retryable": False,
                    },
                    finished_at=utcnow(),
                )
            )
            waiting_rows = (
                await session.scalars(
                    select(NodeRun).where(
                        NodeRun.run_id == run.id,
                        NodeRun.status == "waiting_user",
                    )
                )
            ).all()
            unknown_outcomes = [
                item
                for item in waiting_rows
                if (item.error_json or {}).get("code") == "UNKNOWN_OUTCOME"
            ]
            for item in waiting_rows:
                if item not in unknown_outcomes:
                    item.status = "cancelled"
                    item.finished_at = utcnow()
            active = await session.scalar(
                select(func.count())
                .select_from(NodeRun)
                .where(NodeRun.run_id == run.id, NodeRun.status == "running")
            )
            if unknown_outcomes:
                run.status = "waiting_approval"
            elif not active:
                run.status = "cancelled"
                run.finished_at = utcnow()
                await append_event(session, run.id, "run.cancelled", {"runId": run.id})
            else:
                run.status = "cancelling"
            return run

        latest = await self._latest_node_runs(session, run.id)
        if run.status == "queued":
            run.status = "running"
            run.started_at = utcnow()
            await append_event(session, run.id, "run.started", {"runId": run.id})

        fatal = [
            item
            for node_id, item in latest.items()
            if item.status == "failed" and nodes[node_id].on_error == "fail_run"
        ]
        if fatal:
            await self._finish_failed(session, run, fatal[0].error_json)
            return run

        waiting = [item for item in latest.values() if item.status == "waiting_user"]
        if waiting:
            run.status = "waiting_approval"
            return run
        if run.status == "waiting_approval":
            run.status = "running"

        incoming = {node_id: [] for node_id in nodes}
        for edge in definition.spec.edges:
            incoming[edge.target].append(edge)
        values: dict[str, Any] = {}
        for node_id, item in latest.items():
            if item.status == "succeeded" and item.output_json is not None:
                values[node_id] = item.output_json
            elif item.status == "failed" and nodes[node_id].on_error == "continue":
                values[node_id] = {"error": item.error_json or {}}

        for node in definition.spec.nodes:
            if node.id in latest:
                continue
            edges = incoming[node.id]
            if edges and not all(
                edge.source in latest and latest[edge.source].status in TERMINAL_NODE_STATES
                for edge in edges
            ):
                continue
            selected = [
                edge for edge in edges if self._edge_selected(edge.source, edge.when, latest, nodes)
            ]
            if edges and not selected:
                await self._insert_node_run(
                    session,
                    run,
                    node,
                    status="skipped",
                    inputs={},
                    finished_at=utcnow(),
                )
                continue
            try:
                resolved = resolve_mapping(node.input, run.input_json, values)
                state = "ready"
                error = None
            except ExpressionError as exc:
                resolved = {}
                state = "failed"
                error = {"code": "INVALID_INPUT", "message": str(exc), "retryable": False}
            await self._insert_node_run(
                session,
                run,
                node,
                status=state,
                inputs=resolved,
                error=error,
            )

        await session.flush()
        latest = await self._latest_node_runs(session, run.id)
        fatal = [
            item
            for node_id, item in latest.items()
            if item.status == "failed" and nodes[node_id].on_error == "fail_run"
        ]
        if fatal:
            await self._finish_failed(session, run, fatal[0].error_json)
            return run

        output_node = next(node for node in nodes.values() if node.type == NodeType.OUTPUT)
        output_run = latest.get(output_node.id)
        if output_run and output_run.status == "succeeded":
            run.output_json = output_run.output_json or {}
            errors = list(
                Draft202012Validator(definition.spec.output_schema).iter_errors(run.output_json)
            )
            if errors:
                await self._finish_failed(
                    session,
                    run,
                    {"code": "CONTRACT_VIOLATION", "message": errors[0].message},
                )
            else:
                run.status = "succeeded"
                run.finished_at = utcnow()
                await append_event(
                    session,
                    run.id,
                    "run.completed",
                    {"runId": run.id, "output": run.output_json},
                )
        elif output_run and output_run.status == "skipped":
            await self._finish_failed(
                session,
                run,
                {"code": "NO_OUTPUT", "message": "Output 节点未生成结果"},
            )
        return run

    @staticmethod
    def _duration_exceeded(run: Run, definition: WorkflowDefinition) -> bool:
        started = _aware(run.started_at)
        if not started:
            return False
        return utcnow() >= started + timedelta(
            seconds=definition.spec.policies.max_duration_seconds
        )

    @staticmethod
    def _edge_selected(
        source: str,
        when: bool | None,
        latest: dict[str, NodeRun],
        nodes: dict[str, WorkflowNode],
    ) -> bool:
        source_run = latest[source]
        allowed = source_run.status == "succeeded" or (
            source_run.status == "failed" and nodes[source].on_error == "continue"
        )
        if not allowed:
            return False
        if when is None:
            return True
        return bool((source_run.output_json or {}).get("result")) == when

    @staticmethod
    async def _latest_node_runs(session: AsyncSession, run_id: str) -> dict[str, NodeRun]:
        rows = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run_id)
                .order_by(NodeRun.node_id, NodeRun.attempt.desc())
            )
        ).all()
        latest: dict[str, NodeRun] = {}
        for row in rows:
            latest.setdefault(row.node_id, row)
        return latest

    @staticmethod
    async def _insert_node_run(
        session: AsyncSession,
        run: Run,
        node: WorkflowNode,
        *,
        status: str,
        inputs: dict[str, Any],
        error: dict[str, Any] | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        node_run_id = str(uuid4())
        result = await session.execute(
            sqlite_insert(NodeRun)
            .values(
                id=node_run_id,
                run_id=run.id,
                node_id=node.id,
                node_type=node.type.value,
                attempt=1,
                status=status,
                input_json=inputs,
                error_json=error,
                available_at=utcnow(),
                created_at=utcnow(),
                updated_at=utcnow(),
                finished_at=finished_at,
            )
            .on_conflict_do_nothing(
                index_elements=[NodeRun.run_id, NodeRun.node_id, NodeRun.attempt]
            )
        )
        if result.rowcount:
            event_type = {
                "ready": "node.ready",
                "failed": "node.failed",
                "skipped": "node.skipped",
            }[status]
            await append_event(
                session,
                run.id,
                event_type,
                {"nodeId": node.id, "attempt": 1, "error": error},
                node_run_id,
            )

    @staticmethod
    async def _finish_failed(session: AsyncSession, run: Run, error: dict[str, Any] | None) -> None:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_json = error or {"code": "EXECUTION_FAILED", "message": "运行失败"}
        await append_event(
            session,
            run.id,
            "run.failed",
            {"runId": run.id, "error": run.error_json},
        )


@dataclass(frozen=True)
class ClaimedExecution:
    node_run_id: str
    run_id: str
    node_id: str
    attempt: int
    inputs: dict[str, Any]
    run_input: dict[str, Any]
    node: WorkflowNode
    max_duration_seconds: int


class WorkerService:
    def __init__(self, worker_id: str, lease_seconds: int = 30) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.registry = ExecutorRegistry()
        self.orchestrator = Orchestrator()

    async def recover_expired(self, session: AsyncSession) -> int:
        expired = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.status == "running")
                .where(NodeRun.lease_expires_at < utcnow())
                .order_by(NodeRun.created_at)
            )
        ).all()
        recovered = 0
        for node_run in expired:
            run = await session.get(Run, node_run.run_id)
            if not run:
                continue
            version = await session.get(WorkflowVersion, run.workflow_version_id)
            if not version:
                continue
            definition = WorkflowDefinition.model_validate(version.definition_json)
            node = next(item for item in definition.spec.nodes if item.id == node_run.node_id)
            if node.side_effect and not node.idempotent:
                error = {
                    "code": "UNKNOWN_OUTCOME",
                    "message": "Worker 租约过期，无法确认副作用是否已完成",
                    "retryable": False,
                }
                result = await session.execute(
                    update(NodeRun)
                    .where(
                        NodeRun.id == node_run.id,
                        NodeRun.status == "running",
                        NodeRun.attempt == node_run.attempt,
                    )
                    .values(
                        status="waiting_user",
                        worker_id=None,
                        lease_expires_at=None,
                        error_json=error,
                    )
                )
                if result.rowcount:
                    await self._create_unknown_outcome_approval(session, run, node_run, node, error)
                    recovered += 1
                continue
            result = await session.execute(
                update(NodeRun)
                .where(
                    NodeRun.id == node_run.id,
                    NodeRun.status == "running",
                    NodeRun.attempt == node_run.attempt,
                )
                .values(
                    status="lease_expired",
                    worker_id=None,
                    lease_expires_at=None,
                    error_json={
                        "code": "LEASE_EXPIRED",
                        "message": "Worker 租约过期，已创建新 attempt",
                        "retryable": True,
                    },
                    finished_at=utcnow(),
                )
            )
            if not result.rowcount:
                continue
            next_attempt = node_run.attempt + 1
            if next_attempt <= node.retry.max_attempts:
                await session.execute(
                    sqlite_insert(NodeRun)
                    .values(
                        id=str(uuid4()),
                        run_id=run.id,
                        node_id=node.id,
                        node_type=node.type.value,
                        attempt=next_attempt,
                        status="ready",
                        input_json=node_run.input_json,
                        available_at=utcnow(),
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[NodeRun.run_id, NodeRun.node_id, NodeRun.attempt]
                    )
                )
                await append_event(
                    session,
                    run.id,
                    "node.takeover_ready",
                    {"nodeId": node.id, "attempt": next_attempt},
                )
            else:
                await session.execute(
                    update(NodeRun)
                    .where(NodeRun.id == node_run.id)
                    .values(
                        status="failed",
                        error_json={
                            "code": "LEASE_EXPIRED",
                            "message": "Worker 租约过期且已达到最大 attempt",
                            "retryable": False,
                        },
                    )
                )
            recovered += 1
        return recovered

    async def claim(self, session: AsyncSession) -> ClaimedExecution | None:
        now = utcnow()
        candidates = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.status.in_(["ready", "retry_waiting"]))
                .where(NodeRun.available_at <= now)
                .order_by(NodeRun.created_at)
                .limit(32)
            )
        ).all()
        for candidate in candidates:
            run = await session.get(Run, candidate.run_id)
            if run is None or run.status != "running":
                continue
            version = await session.get(WorkflowVersion, run.workflow_version_id)
            if version is None:
                continue
            definition = WorkflowDefinition.model_validate(version.definition_json)
            max_parallel = definition.spec.policies.max_parallel_nodes
            # 将并发配额检查折叠进领取 UPDATE：running 计数与所有权 CAS 是同一条
            # 原子语句。SQLite 串行化写入，两个 Worker 不会同时越过 Run 级上限。
            running_slots = (
                select(func.count())
                .select_from(NodeRun)
                .where(NodeRun.run_id == run.id, NodeRun.status == "running")
                .scalar_subquery()
            )
            result = await session.execute(
                update(NodeRun)
                .where(
                    NodeRun.id == candidate.id,
                    NodeRun.status.in_(["ready", "retry_waiting"]),
                    NodeRun.attempt == candidate.attempt,
                    running_slots < max_parallel,
                )
                .values(
                    status="running",
                    worker_id=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    heartbeat_at=now,
                    started_at=now,
                )
            )
            if result.rowcount != 1:
                continue
            node = next(item for item in definition.spec.nodes if item.id == candidate.node_id)
            # 运行前用 WorkflowVersion 中锁定的 mcpLock 逐项核对 Tool 契约。
            mcp_lock = next(
                (
                    lock
                    for lock in (version.validation_json or {}).get("mcpLocks", [])
                    if lock.get("nodeId") == node.id
                ),
                None,
            )
            try:
                node = await resolve_skillx_node(session, node)
                node = await resolve_mcp_node(session, node, mcp_lock)
            except MCPLockDriftError as exc:
                await self._fail_claimed_resolution(
                    session, candidate, run, "MCP_CONTRACT_DRIFT", str(exc)
                )
                continue
            except (CapabilityResolutionError, MCPConfigurationError) as exc:
                await self._fail_claimed_resolution(
                    session, candidate, run, "CAPABILITY_INCOMPATIBLE", str(exc)
                )
                continue
            await append_event(
                session,
                candidate.run_id,
                "node.started",
                {"nodeId": candidate.node_id, "attempt": candidate.attempt},
                candidate.id,
            )
            return ClaimedExecution(
                node_run_id=candidate.id,
                run_id=run.id,
                node_id=candidate.node_id,
                attempt=candidate.attempt,
                inputs=candidate.input_json,
                run_input=run.input_json,
                node=node,
                max_duration_seconds=definition.spec.policies.max_duration_seconds,
            )
        return None

    async def _fail_claimed_resolution(
        self,
        session: AsyncSession,
        candidate: NodeRun,
        run: Run,
        code: str,
        message: str,
    ) -> None:
        """领取后解析/契约核对失败：把节点置为 failed 并推进 Run，不执行 Tool。"""

        await session.execute(
            update(NodeRun)
            .where(
                NodeRun.id == candidate.id,
                NodeRun.status == "running",
                NodeRun.worker_id == self.worker_id,
            )
            .values(
                status="failed",
                worker_id=None,
                lease_expires_at=None,
                error_json={"code": code, "message": message, "retryable": False},
                finished_at=utcnow(),
            )
        )
        await append_event(
            session,
            run.id,
            "node.failed",
            {
                "nodeId": candidate.node_id,
                "attempt": candidate.attempt,
                "error": {"code": code, "message": message},
            },
            candidate.id,
        )
        await self.orchestrator.advance_run(session, run.id)

    async def execute_claimed(self, claimed: ClaimedExecution) -> None:
        cancellation = CancellationToken(asyncio.Event())
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claimed, cancellation, stop_heartbeat))
        executor = self.registry.get(claimed.node.type)
        task = asyncio.create_task(
            executor.execute(
                RunContext(
                    run_id=claimed.run_id,
                    run_input=claimed.run_input,
                    node_run_id=claimed.node_run_id,
                    cancellation=cancellation,
                ),
                claimed.node,
                claimed.inputs,
            )
        )
        cancellation_waiter = asyncio.create_task(cancellation.event.wait())
        try:
            done, _ = await asyncio.wait(
                {task, cancellation_waiter},
                timeout=claimed.node.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                result = await task
            elif cancellation_waiter in done:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                result = self._interrupted_result(claimed.node, "CANCELLED", "节点执行已取消")
            else:
                cancellation.event.set()
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                result = self._interrupted_result(claimed.node, "TIMEOUT", "节点执行超时")
        except (ExecutionCancelledError, asyncio.CancelledError):
            result = self._interrupted_result(claimed.node, "CANCELLED", "节点执行已取消")
        finally:
            cancellation_waiter.cancel()
            stop_heartbeat.set()
            await heartbeat
        await self._commit_result(claimed, result)
        async with SessionLocal() as session, session.begin():
            await self.orchestrator.advance_run(session, claimed.run_id)

    async def _heartbeat(
        self,
        claimed: ClaimedExecution,
        cancellation: CancellationToken,
        stop: asyncio.Event,
    ) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            async with SessionLocal() as session, session.begin():
                run = await session.get(Run, claimed.run_id)
                if run is None:
                    cancellation.event.set()
                elif (
                    not run.cancellation_requested_at
                    and _aware(run.started_at)
                    and utcnow()
                    >= _aware(run.started_at) + timedelta(seconds=claimed.max_duration_seconds)
                ):
                    run.status = "cancelling"
                    run.cancellation_requested_at = utcnow()
                    await append_event(
                        session,
                        run.id,
                        "run.timeout_requested",
                        {
                            "runId": run.id,
                            "maxDurationSeconds": claimed.max_duration_seconds,
                        },
                    )
                    cancellation.event.set()
                elif run.cancellation_requested_at:
                    cancellation.event.set()
                result = await session.execute(
                    update(NodeRun)
                    .where(
                        NodeRun.id == claimed.node_run_id,
                        NodeRun.status == "running",
                        NodeRun.worker_id == self.worker_id,
                        NodeRun.attempt == claimed.attempt,
                    )
                    .values(
                        heartbeat_at=utcnow(),
                        lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds),
                    )
                )
                if result.rowcount != 1:
                    cancellation.event.set()

    @staticmethod
    def _interrupted_result(node: WorkflowNode, code: str, message: str) -> NodeResult:
        if node.side_effect and not node.idempotent:
            return NodeResult(
                status="waiting_user",
                error={
                    "code": "UNKNOWN_OUTCOME",
                    "message": f"{message}，无法确认副作用结果",
                    "retryable": False,
                },
            )
        return NodeResult(
            status="cancelled" if code == "CANCELLED" else "failed",
            error={"code": code, "message": message, "retryable": code == "TIMEOUT"},
        )

    async def _commit_result(self, claimed: ClaimedExecution, result: NodeResult) -> None:
        async with SessionLocal() as session, session.begin():
            values: dict[str, Any] = {
                "worker_id": None,
                "lease_expires_at": None,
                "heartbeat_at": utcnow(),
            }
            event_type: str
            event_payload: dict[str, Any]
            if result.status == "succeeded":
                values.update(
                    status="succeeded",
                    output_json=result.output or {},
                    finished_at=utcnow(),
                )
                event_type = "node.completed"
                event_payload = {
                    "nodeId": claimed.node_id,
                    "attempt": claimed.attempt,
                    "metrics": result.metrics,
                }
            elif result.status == "waiting_user":
                values.update(
                    status="waiting_user",
                    output_json=result.output,
                    error_json=result.error.model_dump() if result.error else None,
                )
                event_type = "approval.requested"
                event_payload = {
                    "nodeId": claimed.node_id,
                    "attempt": claimed.attempt,
                    "error": values["error_json"],
                }
            elif result.status == "cancelled":
                values.update(
                    status="cancelled",
                    error_json=result.error.model_dump() if result.error else None,
                    finished_at=utcnow(),
                )
                event_type = "node.cancelled"
                event_payload = {"nodeId": claimed.node_id, "attempt": claimed.attempt}
            else:
                error = (
                    result.error.model_dump()
                    if result.error
                    else {
                        "code": "EXECUTION_FAILED",
                        "message": "节点执行失败",
                        "retryable": False,
                    }
                )
                values.update(status="failed", error_json=error, finished_at=utcnow())
                event_type = "node.failed"
                event_payload = {
                    "nodeId": claimed.node_id,
                    "attempt": claimed.attempt,
                    "error": error,
                }
            cas = await session.execute(
                update(NodeRun)
                .where(
                    NodeRun.id == claimed.node_run_id,
                    NodeRun.status == "running",
                    NodeRun.worker_id == self.worker_id,
                    NodeRun.attempt == claimed.attempt,
                )
                .values(**values)
            )
            if cas.rowcount != 1:
                # 节点已被接管、取消或此结果迟到：清理暂存文件，避免留下孤儿 Artifact。
                clear_staging(claimed.run_id, claimed.node_run_id)
                await append_event(
                    session,
                    claimed.run_id,
                    "node.stale_result_ignored",
                    {"nodeId": claimed.node_id, "attempt": claimed.attempt},
                    claimed.node_run_id,
                )
                return
            await append_event(
                session,
                claimed.run_id,
                event_type,
                event_payload,
                claimed.node_run_id,
            )
            for item in result.events:
                await append_event(
                    session,
                    claimed.run_id,
                    item["type"],
                    item.get("payload", {}),
                    claimed.node_run_id,
                )
            if result.status == "waiting_user":
                await self._create_approval(session, claimed, result)
            for item in result.artifacts:
                # CAS 已成功，先把暂存文件原子落位再登记数据库记录。
                promote_staged_artifact(claimed.run_id, item)
                session.add(
                    Artifact(
                        id=item["id"],
                        run_id=claimed.run_id,
                        kind=item["kind"],
                        name=item["name"],
                        uri=item["uri"],
                        sha256=item["sha256"],
                        size=item["size"],
                        media_type=item["mediaType"],
                        metadata_json={
                            **item.get("metadata", {}),
                            "storageKey": item["storageKey"],
                        },
                    )
                )
                await append_event(
                    session,
                    claimed.run_id,
                    "artifact.created",
                    {
                        "artifactId": item["id"],
                        "name": item["name"],
                        "mediaType": item["mediaType"],
                    },
                    claimed.node_run_id,
                )
            # 无论结果如何，落位后清理该 node_run 的剩余暂存文件。
            clear_staging(claimed.run_id, claimed.node_run_id)
            if result.status == "failed":
                await self._schedule_retry(session, claimed, result)

    async def _schedule_retry(
        self,
        session: AsyncSession,
        claimed: ClaimedExecution,
        result: NodeResult,
    ) -> None:
        error = result.error
        # 统一重试策略：有副作用且非幂等禁止自动重试，结果未知不重试。
        node_effect = "write" if claimed.node.side_effect else "none"
        outcome = "unknown" if (error and error.code == "UNKNOWN_OUTCOME") else None
        if (
            not error
            or not error.retryable
            or claimed.attempt >= claimed.node.retry.max_attempts
            or not auto_retry_allowed(node_effect, claimed.node.idempotent, outcome=outcome)
        ):
            return
        next_attempt = claimed.attempt + 1
        retry_id = str(uuid4())
        inserted = await session.execute(
            sqlite_insert(NodeRun)
            .values(
                id=retry_id,
                run_id=claimed.run_id,
                node_id=claimed.node_id,
                node_type=claimed.node.type.value,
                attempt=next_attempt,
                status="retry_waiting",
                input_json=claimed.inputs,
                available_at=utcnow() + timedelta(seconds=claimed.node.retry.backoff_seconds),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=[NodeRun.run_id, NodeRun.node_id, NodeRun.attempt]
            )
        )
        if inserted.rowcount:
            await append_event(
                session,
                claimed.run_id,
                "node.retry_scheduled",
                {
                    "nodeId": claimed.node_id,
                    "attempt": next_attempt,
                    "error": error.model_dump(),
                },
                retry_id,
            )

    @staticmethod
    async def _create_approval(
        session: AsyncSession,
        claimed: ClaimedExecution,
        result: NodeResult,
    ) -> None:
        snapshot = (result.output or {}).get("approvalSnapshot") or {
            "runId": claimed.run_id,
            "nodeId": claimed.node_id,
            "title": claimed.node.display_name,
            "description": result.error.message if result.error else "需要用户处理",
            "payload": claimed.inputs,
            "effect": "unknown",
        }
        approval = Approval(
            id=str(uuid4()),
            run_id=claimed.run_id,
            node_run_id=claimed.node_run_id,
            node_id=claimed.node_id,
            snapshot_digest=digest_json(snapshot),
            snapshot_json=snapshot,
            status="pending",
        )
        session.add(approval)
        await session.flush()

    @staticmethod
    async def _create_unknown_outcome_approval(
        session: AsyncSession,
        run: Run,
        node_run: NodeRun,
        node: WorkflowNode,
        error: dict[str, Any],
    ) -> None:
        snapshot = {
            "runId": run.id,
            "nodeId": node.id,
            "title": f"{node.display_name}结果未知",
            "description": error["message"],
            "payload": node_run.input_json,
            "effect": node.config.get("effect", "unknown"),
        }
        session.add(
            Approval(
                id=str(uuid4()),
                run_id=run.id,
                node_run_id=node_run.id,
                node_id=node.id,
                snapshot_digest=digest_json(snapshot),
                snapshot_json=snapshot,
                status="pending",
            )
        )
        await append_event(
            session,
            run.id,
            "node.unknown_outcome",
            {"nodeId": node.id, "attempt": node_run.attempt},
            node_run.id,
        )

    async def run_once(self) -> bool:
        with suppress(OperationalError):
            async with SessionLocal() as session, session.begin():
                await self.recover_expired(session)
        try:
            async with SessionLocal() as session, session.begin():
                claimed = await self.claim(session)
        except OperationalError:
            # 并发写入竞争（SQLite 快照冲突/锁定）：本轮不领取，交由下一轮重试。
            return False
        if claimed is None:
            return False
        await self.execute_claimed(claimed)
        return True
