from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.expressions import ExpressionError, resolve_mapping
from app.domain.workflow import NodeResult, NodeType, WorkflowDefinition
from app.infrastructure.database import (
    NodeRun,
    Run,
    RunEvent,
    SessionLocal,
    WorkflowVersion,
    utcnow,
)
from app.runtime.executors import ExecutorRegistry, RunContext

TERMINAL_NODE_STATES = {"succeeded", "failed", "skipped", "cancelled"}


async def append_event(
    session: AsyncSession,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
    node_run_id: str | None = None,
) -> RunEvent:
    current = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    event = RunEvent(
        id=str(uuid4()),
        run_id=run_id,
        node_run_id=node_run_id,
        seq=(current or 0) + 1,
        type=event_type,
        payload_json=payload,
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

    async def advance_run(self, session: AsyncSession, run_id: str) -> Run:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError("run not found")
        if run.status in {"succeeded", "failed", "cancelled", "partially_succeeded"}:
            return run
        if run.cancellation_requested_at:
            await session.execute(
                update(NodeRun)
                .where(NodeRun.run_id == run.id)
                .where(NodeRun.status.in_(["pending", "ready", "retry_waiting"]))
                .values(status="cancelled", finished_at=utcnow())
            )
            run.status = "cancelled"
            run.finished_at = utcnow()
            await append_event(session, run.id, "run.cancelled", {"runId": run.id})
            return run

        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:
            raise LookupError("workflow version not found")
        definition = WorkflowDefinition.model_validate(version.definition_json)
        nodes = {node.id: node for node in definition.spec.nodes}
        node_runs = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run.id)
                .order_by(NodeRun.node_id, NodeRun.attempt.desc())
            )
        ).all()
        latest: dict[str, NodeRun] = {}
        for node_run in node_runs:
            latest.setdefault(node_run.node_id, node_run)

        if run.status == "queued":
            run.status = "running"
            run.started_at = utcnow()
            await append_event(session, run.id, "run.started", {"runId": run.id})

        failed = [item for item in latest.values() if item.status == "failed"]
        if failed:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_json = failed[0].error_json
            await append_event(
                session,
                run.id,
                "run.failed",
                {"runId": run.id, "error": run.error_json},
            )
            return run

        waiting = [item for item in latest.values() if item.status == "waiting_user"]
        if waiting:
            run.status = "waiting_approval"
        elif run.status == "waiting_approval":
            run.status = "running"

        incoming = {node_id: [] for node_id in nodes}
        for edge in definition.spec.edges:
            incoming[edge.target].append(edge)
        outputs = {
            node_id: item.output_json
            for node_id, item in latest.items()
            if item.status == "succeeded" and item.output_json is not None
        }

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
                edge
                for edge in edges
                if latest[edge.source].status == "succeeded"
                and (
                    edge.when is None
                    or bool((latest[edge.source].output_json or {}).get("result")) == edge.when
                )
            ]
            if edges and not selected:
                skipped = NodeRun(
                    id=str(uuid4()),
                    run_id=run.id,
                    node_id=node.id,
                    node_type=node.type.value,
                    attempt=1,
                    status="skipped",
                    input_json={},
                    finished_at=utcnow(),
                )
                session.add(skipped)
                latest[node.id] = skipped
                await append_event(
                    session,
                    run.id,
                    "node.skipped",
                    {"nodeId": node.id},
                    skipped.id,
                )
                continue
            if any(latest[edge.source].status != "succeeded" for edge in selected):
                continue
            try:
                resolved = resolve_mapping(node.input, run.input_json, outputs)
            except ExpressionError as exc:
                resolved = {}
                state = "failed"
                error = {"code": "INVALID_INPUT", "message": str(exc), "retryable": False}
            else:
                state = "ready"
                error = None
            node_run = NodeRun(
                id=str(uuid4()),
                run_id=run.id,
                node_id=node.id,
                node_type=node.type.value,
                attempt=1,
                status=state,
                input_json=resolved,
                error_json=error,
            )
            session.add(node_run)
            latest[node.id] = node_run
            await append_event(
                session,
                run.id,
                "node.ready" if state == "ready" else "node.failed",
                {"nodeId": node.id, "attempt": 1, "error": error},
                node_run.id,
            )

        await session.flush()
        newly_failed = [item for item in latest.values() if item.status == "failed"]
        if newly_failed:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_json = newly_failed[0].error_json
            await append_event(
                session,
                run.id,
                "run.failed",
                {"runId": run.id, "error": run.error_json},
            )
            return run
        latest_states = {item.node_id: item.status for item in latest.values()}
        output_nodes = [node for node in nodes.values() if node.type == NodeType.OUTPUT]
        if output_nodes and all(
            latest_states.get(node.id) in {"succeeded", "skipped"} for node in output_nodes
        ):
            succeeded_outputs = [
                latest[node.id].output_json
                for node in output_nodes
                if latest[node.id].status == "succeeded"
            ]
            run.output_json = succeeded_outputs[-1] if succeeded_outputs else {}
            output_errors = list(
                Draft202012Validator(definition.spec.output_schema).iter_errors(run.output_json)
            )
            if output_errors:
                run.status = "failed"
                run.error_json = {
                    "code": "CONTRACT_VIOLATION",
                    "message": output_errors[0].message,
                }
                await append_event(
                    session, run.id, "run.failed", {"runId": run.id, "error": run.error_json}
                )
            else:
                run.status = "succeeded"
                await append_event(
                    session, run.id, "run.completed", {"runId": run.id, "output": run.output_json}
                )
            run.finished_at = utcnow()
        return run


class WorkerService:
    def __init__(self, worker_id: str, lease_seconds: int = 30) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.registry = ExecutorRegistry()
        self.orchestrator = Orchestrator()

    async def recover_expired(self, session: AsyncSession) -> int:
        result = await session.execute(
            update(NodeRun)
            .where(NodeRun.status == "running")
            .where(NodeRun.lease_expires_at < utcnow())
            .values(
                status="ready",
                worker_id=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )
        return result.rowcount or 0

    async def claim(self, session: AsyncSession) -> NodeRun | None:
        now = utcnow()
        candidate = await session.scalar(
            select(NodeRun)
            .where(NodeRun.status.in_(["ready", "retry_waiting"]))
            .where(NodeRun.available_at <= now)
            .order_by(NodeRun.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        result = await session.execute(
            update(NodeRun)
            .where(NodeRun.id == candidate.id)
            .where(NodeRun.status.in_(["ready", "retry_waiting"]))
            .values(
                status="running",
                worker_id=self.worker_id,
                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                heartbeat_at=now,
                started_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        await session.refresh(candidate)
        await append_event(
            session,
            candidate.run_id,
            "node.started",
            {"nodeId": candidate.node_id, "attempt": candidate.attempt},
            candidate.id,
        )
        return candidate

    async def execute_claimed(self, session: AsyncSession, node_run: NodeRun) -> None:
        run = await session.get(Run, node_run.run_id)
        if run is None:
            return
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:
            return
        definition = WorkflowDefinition.model_validate(version.definition_json)
        node = next(item for item in definition.spec.nodes if item.id == node_run.node_id)
        executor = self.registry.get(node.type)
        try:
            result = await asyncio.wait_for(
                executor.execute(RunContext(session, run, node_run), node, node_run.input_json),
                timeout=node.timeout_seconds,
            )
        except TimeoutError:
            result = NodeResult(
                status="failed",
                error={"code": "TIMEOUT", "message": "节点执行超时", "retryable": True},
            )
        if result.status == "succeeded":
            node_run.status = "succeeded"
            node_run.output_json = result.output or {}
            node_run.finished_at = utcnow()
            await append_event(
                session,
                run.id,
                "node.completed",
                {"nodeId": node.id, "output": node_run.output_json, "metrics": result.metrics},
                node_run.id,
            )
        elif result.status == "waiting_user":
            node_run.status = "waiting_user"
            node_run.output_json = result.output
            node_run.error_json = result.error.model_dump() if result.error else None
            await append_event(
                session,
                run.id,
                "approval.requested",
                {"nodeId": node.id, "output": result.output, "error": node_run.error_json},
                node_run.id,
            )
        else:
            await self._fail_or_retry(session, run, node, node_run, result)
        node_run.worker_id = None
        node_run.lease_expires_at = None
        await self.orchestrator.advance_run(session, run.id)

    async def _fail_or_retry(
        self,
        session: AsyncSession,
        run: Run,
        node: Any,
        node_run: NodeRun,
        result: NodeResult,
    ) -> None:
        error = (
            result.error.model_dump()
            if result.error
            else {
                "code": "INTERNAL_ERROR",
                "message": "节点执行失败",
                "retryable": False,
            }
        )
        node_run.status = "failed"
        node_run.error_json = error
        node_run.finished_at = utcnow()
        can_retry = bool(error.get("retryable")) and node_run.attempt < node.retry.max_attempts
        if can_retry:
            retry = NodeRun(
                id=str(uuid4()),
                run_id=run.id,
                node_id=node.id,
                node_type=node.type.value,
                attempt=node_run.attempt + 1,
                status="retry_waiting",
                input_json=node_run.input_json,
                available_at=utcnow() + timedelta(seconds=node.retry.backoff_seconds),
            )
            session.add(retry)
            await append_event(
                session,
                run.id,
                "node.retry_scheduled",
                {"nodeId": node.id, "attempt": retry.attempt, "error": error},
                retry.id,
            )
        else:
            await append_event(
                session,
                run.id,
                "node.failed",
                {"nodeId": node.id, "attempt": node_run.attempt, "error": error},
                node_run.id,
            )

    async def run_once(self) -> bool:
        async with SessionLocal() as session:
            async with session.begin():
                await self.recover_expired(session)
                claimed = await self.claim(session)
            if claimed is None:
                return False
            async with session.begin():
                current = await session.get(NodeRun, claimed.id)
                if current and current.status == "running" and current.worker_id == self.worker_id:
                    await self.execute_claimed(session, current)
            return True
