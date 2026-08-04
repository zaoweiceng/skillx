from __future__ import annotations

import asyncio
import logging
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
from app.config import get_settings
from app.domain.workflow import NodeResult, NodeType, WorkflowDefinition, WorkflowNode
from app.infrastructure.database import (
    Approval,
    Artifact,
    ConversationRun,
    ExecutionCheckpoint,
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
    list_final_files,
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

logger = logging.getLogger(__name__)


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
            was_recovering = run.status == "recovering"
            run.status = "cancelling"
            run.cancellation_requested_at = utcnow()
            await append_event(session, run.id, "run.cancellation_requested", {"runId": run.id})
            # recovering 状态被取消：把尚未消费的 pending checkpoint 一并置为 cancelled，
            # 避免 RecoveryDriver 在 TOCTOU 窗口内继续把已取消的 Run 拉回 running。
            if was_recovering:
                await session.execute(
                    update(ExecutionCheckpoint)
                    .where(
                        ExecutionCheckpoint.run_id == run.id,
                        ExecutionCheckpoint.status.in_(["pending", "running"]),
                    )
                    .values(status="cancelled")
                )
        await self.advance_run(session, run.id)
        return run

    async def advance_run(self, session: AsyncSession, run_id: str) -> Run:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError("run not found")
        if run.status in FINAL_RUN_STATES:
            return run
        # recovering 状态由恢复驱动独占：advance_run 不再处理，避免把等待恢复的 run
        # 误判为 fatal 后 fail run。
        if run.status == "recovering":
            return run
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if version is None:
            raise LookupError("workflow version not found")
        definition = WorkflowDefinition.model_validate(version.definition_json)
        nodes = {node.id: node for node in definition.spec.nodes}
        # fallback-only target 只在恢复时激活：正常调度绝不为其创建 NodeRun。
        from app.application.recovery import _fallback_only_targets, load_substitutions

        fallback_only = _fallback_only_targets(definition)
        # 恢复替代输出（不可变历史）：下游据此读取被替代节点的输出，失败 NodeRun 不改写。
        substitutions = await load_substitutions(session, run.id)
        # 处于活跃恢复中的失败 attempt：不得被再次判为 fatal（正在等待审批或恢复中）。
        active_checkpoints = (
            await session.scalars(
                select(ExecutionCheckpoint).where(
                    ExecutionCheckpoint.run_id == run.id,
                    ExecutionCheckpoint.status.in_(["pending", "running", "awaiting_user"]),
                )
            )
        ).all()
        under_recovery = {item.failed_node_run_id for item in active_checkpoints}

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
            if item.status == "failed"
            and nodes[node_id].on_error == "fail_run"
            and node_id not in substitutions
            and item.id not in under_recovery
        ]
        if fatal:
            await self._handle_fatal(session, run, definition, fatal[0])
            return run

        waiting = [item for item in latest.values() if item.status == "waiting_user"]
        if waiting:
            run.status = "waiting_approval"
            return run
        # 等待恢复审批：保持 waiting_approval，直到人工批准（→recovering）或拒绝（→failed）。
        if any(item.approval_status == "pending" for item in active_checkpoints):
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
        # 恢复替代输出：被替代的失败节点以其替代值参与下游解析（原失败 NodeRun 不改写）。
        for node_id, output in substitutions.items():
            if output:
                values[node_id] = output

        for node in definition.spec.nodes:
            # fallback-only target 只在恢复时由 RecoveryDriver 创建，正常调度跳过。
            if node.id in fallback_only:
                continue
            if node.id in latest:
                continue
            edges = incoming[node.id]
            if edges and not all(
                self._edge_ready(edge.source, latest, substitutions) for edge in edges
            ):
                continue
            selected = [
                edge
                for edge in edges
                if self._edge_selected(edge.source, edge.when, latest, nodes, substitutions)
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
            if item.status == "failed"
            and nodes[node_id].on_error == "fail_run"
            and node_id not in substitutions
            and item.id not in under_recovery
        ]
        if fatal:
            await self._handle_fatal(session, run, definition, fatal[0])
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
    def _edge_ready(
        source: str,
        latest: dict[str, NodeRun],
        substitutions: dict[str, Any],
    ) -> bool:
        """上游节点是否处于终态（可解析下游）：正常终态，或被恢复替代的失败节点。"""

        if source not in latest:
            return False
        if source in substitutions and latest[source].status == "failed":
            return True
        return latest[source].status in TERMINAL_NODE_STATES

    @staticmethod
    def _edge_selected(
        source: str,
        when: bool | None,
        latest: dict[str, NodeRun],
        nodes: dict[str, WorkflowNode],
        substitutions: dict[str, Any] | None = None,
    ) -> bool:
        source_run = latest[source]
        substitutions = substitutions or {}
        substituted = source in substitutions and source_run.status == "failed"
        allowed = (
            source_run.status == "succeeded"
            or substituted
            or (source_run.status == "failed" and nodes[source].on_error == "continue")
        )
        if not allowed:
            return False
        if when is None:
            return True
        output = substitutions[source] if substituted else (source_run.output_json or {})
        return bool((output or {}).get("result")) == when

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

    async def _handle_fatal(
        self,
        session: AsyncSession,
        run: Run,
        definition: WorkflowDefinition,
        failed_node_run: NodeRun,
    ) -> None:
        """致命失败处理：命中显式 fallback 时冻结 checkpoint。

        - 可自动恢复（错误码在 fallback.on 且非危险类）且无需审批 → ``recovering``；
        - ``requireUserApproval=true`` → 先建 checkpoint 并进入 ``waiting_approval``，
          等待人工批准/拒绝，不直接失败（A-03 / P2-3）；
        - 否则按原语义 fail run。Direct/MCP/Browser 失败不隐式升级为 Agent。
        """

        from app.application.recovery import build_checkpoint, is_recoverable

        node = next(
            (item for item in definition.spec.nodes if item.id == failed_node_run.node_id),
            None,
        )
        error_code = (failed_node_run.error_json or {}).get("code")
        fallback = node.fallback if node else None
        # 已有该失败 attempt 的 checkpoint 则不重复创建（幂等）。
        existing = await session.scalar(
            select(ExecutionCheckpoint).where(
                ExecutionCheckpoint.run_id == run.id,
                ExecutionCheckpoint.failed_node_run_id == failed_node_run.id,
            )
        )
        hits_fallback = (
            fallback is not None
            and existing is None
            and error_code is not None
            and error_code in fallback.on
        )
        if hits_fallback and fallback.require_user_approval:
            # 危险错误先建立 checkpoint，进入待审批；批准后走同一 Recovery claim。
            checkpoint = await build_checkpoint(session, run, definition, failed_node_run)
            checkpoint.approval_status = "pending"
            run.status = "waiting_approval"
            await self._create_recovery_approval(
                session, run, failed_node_run, node, checkpoint, error_code
            )
            await append_event(
                session,
                run.id,
                "run.recovery_approval_requested",
                {
                    "runId": run.id,
                    "checkpointId": checkpoint.id,
                    "failedNode": failed_node_run.node_id,
                    "errorCode": error_code,
                    "fallbackTarget": fallback.target,
                },
            )
            return
        if hits_fallback and is_recoverable(error_code, fallback.on):
            checkpoint = await build_checkpoint(session, run, definition, failed_node_run)
            run.status = "recovering"
            await append_event(
                session,
                run.id,
                "run.recovery_requested",
                {
                    "runId": run.id,
                    "checkpointId": checkpoint.id,
                    "failedNode": failed_node_run.node_id,
                    "errorCode": error_code,
                    "fallbackTarget": fallback.target,
                },
            )
            return
        # 记录失败观测（受控 WorkflowPatch 的后端事实来源）：不进入恢复的致命失败按
        # fingerprint 聚合，供 eligibility 计算是否可复现。
        await self._record_observation(session, run, failed_node_run)
        await self._finish_failed(session, run, failed_node_run.error_json)

    @staticmethod
    async def _record_observation(
        session: AsyncSession, run: Run, failed_node_run: NodeRun
    ) -> None:
        from app.application.workflow_patch import record_failure_observation

        error = failed_node_run.error_json or {}
        with suppress(Exception):
            await record_failure_observation(
                session,
                workflow_version_id=run.workflow_version_id,
                node_id=failed_node_run.node_id,
                node_type=failed_node_run.node_type,
                error_code=error.get("code", "EXECUTION_FAILED"),
                run_id=run.id,
            )

    @staticmethod
    async def _create_recovery_approval(
        session: AsyncSession,
        run: Run,
        failed_node_run: NodeRun,
        node: WorkflowNode | None,
        checkpoint: ExecutionCheckpoint,
        error_code: str | None,
    ) -> None:
        """为审批型 fallback 创建审批：展示错误码、能力集合、已提交副作用与上下文摘要。"""

        snapshot = {
            "runId": run.id,
            "nodeId": failed_node_run.node_id,
            "title": f"恢复审批：{node.display_name if node else failed_node_run.node_id}",
            "description": f"节点失败（{error_code}），是否批准交给受限恢复 Agent？",
            "payload": {
                "checkpointId": checkpoint.id,
                "errorCode": error_code,
                "allowedCapabilities": checkpoint.snapshot_json.get("fallbackConfig", {}).get(
                    "allowedCapabilities", []
                ),
                "committedSideEffects": checkpoint.snapshot_json.get("committedSideEffects", []),
            },
            "effect": "recovery.approval",
            "checkpointId": checkpoint.id,
        }
        session.add(
            Approval(
                id=str(uuid4()),
                run_id=run.id,
                node_run_id=failed_node_run.id,
                node_id=failed_node_run.node_id,
                snapshot_digest=digest_json(snapshot),
                snapshot_json=snapshot,
                status="pending",
            )
        )
        await session.flush()


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
        except Exception:  # noqa: BLE001
            # Executor 抛出的非约定异常（Pydantic/KeyError 等）不得击穿 Worker 循环：
            # 转换为结构化 EXECUTOR_INTERNAL_ERROR，记录 traceback，走标准失败/重试状态机。
            logger.exception(
                "executor raised for node %s (run %s)", claimed.node_id, claimed.run_id
            )
            result = self._interrupted_result(
                claimed.node, "EXECUTOR_INTERNAL_ERROR", "节点执行器内部错误"
            )
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
                # 先登记数据库记录（引用最终 storageKey），暂不落位文件。
                # 文件在事务提交后再从暂存原子落位：提交失败则文件从未离开暂存，
                # 不会在最终目录留下孤儿。
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
                            # 保留 stagingKey：提交后落位若中断，GC 可据此从暂存补落位。
                            "stagingKey": item.get("stagingKey"),
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
            if result.status == "failed":
                await self._schedule_retry(session, claimed, result)
        # 事务已提交：现在把暂存文件原子落位。提交后落位保证“DB 有记录、文件缺失”
        # 可由 GC 从暂存补落位，而“文件已落位、DB 未提交”这一孤儿窗口不再存在。
        for item in result.artifacts:
            promote_staged_artifact(claimed.run_id, item)
        # 落位后清理该 node_run 的剩余暂存文件。
        clear_staging(claimed.run_id, claimed.node_run_id)

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


async def reconcile_run_artifacts(session: AsyncSession, run_id: str) -> dict[str, list[str]]:
    """Artifact 一致性 GC：双向扫描“DB 有记录但文件缺失”和“文件无 DB 记录”。

    - missingFile：数据库登记了 Artifact 但最终文件缺失（提交后落位未完成）。若暂存中
      仍有源文件则补落位，否则报告为需人工处理的缺失。
    - orphanFile：最终目录存在但无对应 Artifact 记录（DB 回滚后残留），删除以回收。
    返回各类处理结果，便于审计。
    """

    artifacts = (
        await session.scalars(select(Artifact).where(Artifact.run_id == run_id))
    ).all()
    recorded_keys = {
        (item.metadata_json or {}).get("storageKey") or item.uri.removeprefix("artifact://")
        for item in artifacts
    }
    files = list_final_files(run_id)
    recovered: list[str] = []
    missing: list[str] = []
    for item in artifacts:
        storage_key = (item.metadata_json or {}).get("storageKey") or item.uri.removeprefix(
            "artifact://"
        )
        if storage_key in files:
            continue
        # 尝试从暂存补落位；stagingKey 由执行器写入 metadata 时可能缺失。
        staging_key = (item.metadata_json or {}).get("stagingKey")
        if staging_key:
            promote_staged_artifact(
                run_id, {"storageKey": storage_key, "stagingKey": staging_key}
            )
        if storage_key in list_final_files(run_id):
            recovered.append(item.id)
        else:
            missing.append(item.id)
    orphan_files = sorted(files - {key for key in recorded_keys if key})
    for name in orphan_files:
        target = (get_settings().artifact_root / run_id / name).resolve()
        with suppress(FileNotFoundError):
            target.unlink()
    return {"recovered": recovered, "missing": missing, "orphanFilesRemoved": orphan_files}


async def reconcile_recent_runs(session: AsyncSession, *, limit: int = 50) -> dict[str, Any]:
    """扫描最近若干个 Run 并调用 Artifact reconcile，补落位提交后未完成的文件。

    Worker 启动与周期任务调用；限制批量大小，避免全表扫描。同时补落位 Conversation Agent
    轨迹（成功但落位未完成的 staging）。
    """

    run_ids = (
        await session.scalars(
            select(Run.id).order_by(Run.created_at.desc()).limit(limit)
        )
    ).all()
    recovered = 0
    for run_id in run_ids:
        report = await reconcile_run_artifacts(session, run_id)
        recovered += len(report["recovered"])
    # Conversation Agent 轨迹：成功 run 若 staging 仍有文件，补落位。
    from app.application.agent_run import AgentRunWorker

    conversation_runs = (
        await session.scalars(
            select(ConversationRun.id)
            .where(ConversationRun.status == "succeeded")
            .order_by(ConversationRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    for cr_id in conversation_runs:
        AgentRunWorker._promote_trajectory(cr_id)
    return {"runsScanned": len(run_ids), "artifactsRecovered": recovered}
