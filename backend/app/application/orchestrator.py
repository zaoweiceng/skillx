from __future__ import annotations

import asyncio
import json
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

from app.application.artifact_integrity import (
    ArtifactPathError,
    artifact_record_matches,
    safe_artifact_path,
)
from app.application.capabilities import (
    CapabilityResolutionError,
    resolve_skillx_node,
    validate_workflow_capabilities,
)
from app.application.compiler import digest_json
from app.application.error_policy import error_policy, persisted_error
from app.application.execution_sources import ExecutionSource as ExecutionSource
from app.application.execution_sources import _merge_recovery_plan as _merge_recovery_plan
from app.application.execution_sources import load_execution_source as load_execution_source
from app.application.expressions import ExpressionError, resolve_mapping
from app.application.mcp import MCPConfigurationError, MCPLockDriftError, resolve_mcp_node
from app.application.retries import auto_retry_allowed
from app.application.run_events import append_event as append_event
from app.application.runtime_profiles import agent_snapshot, workflow_profiles
from app.application.security import redact
from app.application.structured_generation import runtime_snapshot_for_definition
from app.config import get_settings
from app.domain.workflow import NodeResult, NodeType, WorkflowDefinition, WorkflowNode
from app.infrastructure.database import (
    Approval,
    Artifact,
    ConversationRun,
    ExecutionCheckpoint,
    FailureObservation,
    NodeRun,
    PlanSnapshot,
    RecoveryAttempt,
    ReplayFixture,
    Run,
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
    write_json_artifact,
)
from app.runtime.state_machine import (
    edge_ready,
    edge_selected,
    node_readiness,
    output_schema_errors,
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


class Orchestrator:
    async def create_run(
        self,
        session: AsyncSession,
        workflow_version: WorkflowVersion,
        inputs: dict[str, Any],
        component_id: str | None = None,
        owner_id: str = "local-user",
        turn_id: str | None = None,
        automatic_recovery: bool = False,
    ) -> Run:
        from app.application.capability_availability import require_version_enabled
        await require_version_enabled(session, workflow_version)
        definition = WorkflowDefinition.model_validate(workflow_version.definition_json)
        await validate_workflow_capabilities(session, definition)
        from app.application.schema_defaults import apply_input_defaults
        inputs = apply_input_defaults(inputs, definition.spec.input_schema)
        errors = list(Draft202012Validator(definition.spec.input_schema).iter_errors(inputs))
        if errors:
            raise ValueError(errors[0].message)
        run = Run(
            id=str(uuid4()),
            workflow_version_id=workflow_version.id,
            component_id=component_id,
            turn_id=turn_id,
            owner_id=owner_id,
            status="queued",
            input_json=inputs,
            runtime_snapshot_json={
                **runtime_snapshot_for_definition(definition),
                **({"automaticRecovery": {
                    "agentRuntimeSnapshot": await agent_snapshot(session, None)
                }} if automatic_recovery else {}),
                "nodeProfiles": (
                    workflow_version.validation_json["nodeProfiles"]
                    if "nodeProfiles" in (workflow_version.validation_json or {}) and not any(
                        profile.get("runtimeSelection") == "direct_llm"
                        for profile in workflow_version.validation_json["nodeProfiles"].values()
                    )
                    else await workflow_profiles(session, definition)
                ),
            },
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

    async def create_plan_run(
        self,
        session: AsyncSession,
        snapshot: PlanSnapshot,
        inputs: dict[str, Any],
        owner_id: str = "local-user",
        turn_id: str | None = None,
        automatic_recovery: bool = False,
    ) -> Run:
        from app.application.plans import verify_plan_snapshot

        verify_plan_snapshot(snapshot)
        definition = WorkflowDefinition.model_validate(snapshot.definition_json)
        from app.application.schema_defaults import apply_input_defaults
        inputs = apply_input_defaults(inputs, definition.spec.input_schema)
        errors = list(Draft202012Validator(definition.spec.input_schema).iter_errors(inputs))
        if errors:
            raise ValueError(errors[0].message)
        run = Run(
            id=str(uuid4()),
            workflow_version_id=None,
            plan_snapshot_id=snapshot.id,
            owner_id=owner_id,
            turn_id=turn_id,
            status="queued",
            input_json=inputs,
            runtime_snapshot_json={
                **runtime_snapshot_for_definition(definition),
                **({"automaticRecovery": {
                    "agentRuntimeSnapshot": await agent_snapshot(session, None)
                }} if automatic_recovery else {}),
                "nodeProfiles": await workflow_profiles(session, definition),
            },
        )
        session.add(run)
        await session.flush()
        await append_event(
            session,
            run.id,
            "run.created",
            {
                "runId": run.id,
                "planSnapshotId": snapshot.id,
                "planDigest": snapshot.digest,
            },
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
        source = await load_execution_source(session, run)
        if source is None:
            raise LookupError("execution source not found")
        definition = WorkflowDefinition.model_validate(source.definition_json)
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
        # 等待恢复审批：保持 waiting_approval，直到人工批准（→recovering）或拒绝（→failed）。
        recovery_wait = any(
            item.approval_status == "pending" or item.status == "awaiting_user"
            for item in active_checkpoints
        )
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
        statuses = {
            node_id: "waiting_user" if item.id in under_recovery else item.status
            for node_id, item in latest.items()
        }
        on_errors = {node_id: node.on_error for node_id, node in nodes.items()}
        raw_outputs = {
            node_id: item.output_json or {} for node_id, item in latest.items()
        }

        for node in definition.spec.nodes:
            # fallback-only target 只在恢复时由 RecoveryDriver 创建，正常调度跳过。
            if node.id in fallback_only:
                continue
            if node.id in latest:
                continue
            edges = incoming[node.id]
            readiness, _selected = node_readiness(
                edges, statuses, raw_outputs, on_errors, substitutions
            )
            if readiness == "waiting":
                continue
            if readiness == "skipped":
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

        active = any(item.status in {"ready", "running", "retry_waiting"}
                     for item in latest.values())
        paused = bool(waiting or recovery_wait) and not active
        self._set_wait_clock(run, paused)
        if paused:
            run.status = "waiting_approval"
            return run

        output_node = next(node for node in nodes.values() if node.type == NodeType.OUTPUT)
        output_run = latest.get(output_node.id)
        if output_run and output_run.status == "succeeded":
            run.output_json = output_run.output_json or {}
            errors = output_schema_errors(definition.spec.output_schema, run.output_json)
            if errors:
                await self._finish_failed(
                    session,
                    run,
                    {"code": "CONTRACT_VIOLATION", "message": errors[0]},
                )
            else:
                run.status = "succeeded"
                run.finished_at = utcnow()
                await self._finalize_recovery_state(session, run, "succeeded")
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
        snapshot = run.runtime_snapshot_json or {}
        paused = snapshot.get("pausedSeconds", 0)
        if snapshot.get("waitingSince"):
            paused += (utcnow() - datetime.fromisoformat(snapshot["waitingSince"])).total_seconds()
        return utcnow() >= started + timedelta(
            seconds=definition.spec.policies.max_duration_seconds + paused
        )

    @staticmethod
    def _set_wait_clock(run: Run, waiting: bool) -> None:
        snapshot = dict(run.runtime_snapshot_json or {})
        if waiting and not snapshot.get("waitingSince"):
            snapshot["waitingSince"] = utcnow().isoformat()
        elif not waiting and snapshot.get("waitingSince"):
            snapshot["pausedSeconds"] = snapshot.get("pausedSeconds", 0) + (
                utcnow() - datetime.fromisoformat(snapshot.pop("waitingSince"))
            ).total_seconds()
        run.runtime_snapshot_json = snapshot

    @staticmethod
    def _edge_ready(
        source: str,
        latest: dict[str, NodeRun],
        substitutions: dict[str, Any],
    ) -> bool:
        """上游节点是否处于终态（可解析下游）：正常终态，或被恢复替代的失败节点。"""

        return edge_ready(
            source,
            {node_id: item.status for node_id, item in latest.items()},
            substitutions,
        )

    @staticmethod
    def _edge_selected(
        source: str,
        when: bool | None,
        latest: dict[str, NodeRun],
        nodes: dict[str, WorkflowNode],
        substitutions: dict[str, Any] | None = None,
    ) -> bool:
        substitutions = substitutions or {}
        return edge_selected(
            source,
            when,
            {node_id: item.status for node_id, item in latest.items()},
            {node_id: item.output_json or {} for node_id, item in latest.items()},
            {node_id: node.on_error for node_id, node in nodes.items()},
            substitutions,
        )

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
        durable_error = (
            persisted_error(
                error,
                technical_detail_ref=f"run:{run.id}:node:{node_run_id}",
            )
            if error is not None
            else None
        )
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
                error_json=durable_error,
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
                {"nodeId": node.id, "attempt": 1, "error": durable_error},
                node_run_id,
            )

    @staticmethod
    async def _finish_failed(session: AsyncSession, run: Run, error: dict[str, Any] | None) -> None:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_json = persisted_error(
            error,
            technical_detail_ref=f"run:{run.id}",
        )
        await Orchestrator._finalize_recovery_state(session, run, "failed")
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
        """Freeze explicit or task-owned recovery without changing the published graph."""

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
            await self._record_observation(
                session, run, failed_node_run, checkpoint_id=checkpoint.id
            )
            checkpoint.approval_status = "pending"
            self._set_wait_clock(run, True)
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
            await self._record_observation(
                session, run, failed_node_run, checkpoint_id=checkpoint.id
            )
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
        automatic = (run.runtime_snapshot_json or {}).get("automaticRecovery")
        if automatic and fallback is None and existing is None and error_code:
            count = await session.scalar(
                select(func.count()).select_from(ExecutionCheckpoint).where(
                    ExecutionCheckpoint.run_id == run.id
                )
            )
            # No model can establish whether an uncertain external operation committed.
            unsafe = {
                "UNKNOWN_OUTCOME", "PERMISSION_DENIED", "AUTHENTICATION_FAILED",
                "CONFIRMATION_DENIED", "RECOVERY_REJECTED", "MODEL_AUTH_FAILED",
                "MODEL_NOT_CONFIGURED", "MODEL_RUNTIME_UNAVAILABLE", "TRAE_AUTH_REQUIRED",
                "TRAE_NOT_INSTALLED", "MODEL_RUNTIME_CONFIG_DRIFT", "APPROVAL_SNAPSHOT_CHANGED",
            }
            if count < 3 and error_code not in unsafe:
                checkpoint = await build_checkpoint(session, run, definition, failed_node_run)
                snapshot = dict(checkpoint.snapshot_json)
                snapshot["fallbackConfig"] = {
                    **automatic, "automatic": True, "allowedCapabilities": [],
                    "maxSteps": 4, "maxTotalTokens": get_settings().agent_max_total_tokens,
                }
                checkpoint.snapshot_json = snapshot
                checkpoint.digest = digest_json(snapshot)
                await self._record_observation(
                    session, run, failed_node_run, checkpoint_id=checkpoint.id
                )
                run.status = "recovering"
                await append_event(session, run.id, "run.recovery_requested", {
                    "runId": run.id, "checkpointId": checkpoint.id,
                    "failedNode": failed_node_run.node_id, "errorCode": error_code,
                    "automatic": True,
                })
                return
        # 记录失败观测（受控 WorkflowPatch 的后端事实来源）：不进入恢复的致命失败按
        # fingerprint 聚合，供 eligibility 计算是否可复现。
        await self._record_observation(session, run, failed_node_run)
        await self._finish_failed(session, run, failed_node_run.error_json)

    @staticmethod
    async def _record_observation(
        session: AsyncSession,
        run: Run,
        failed_node_run: NodeRun,
        *,
        checkpoint_id: str | None = None,
    ) -> None:
        from app.application.workflow_patch import record_failure_observation

        error = failed_node_run.error_json or {}
        with suppress(Exception):
            source = await load_execution_source(session, run)
            await record_failure_observation(
                session,
                workflow_version_id=run.workflow_version_id,
                plan_snapshot_id=run.plan_snapshot_id,
                node_id=failed_node_run.node_id,
                node_type=failed_node_run.node_type,
                error_code=error.get("code", "EXECUTION_FAILED"),
                run_id=run.id,
                source_node_run_id=failed_node_run.id,
                checkpoint_id=checkpoint_id,
                locks=(source.validation_json.get("capabilityLocks", []) if source else []),
                contract_summary={
                    "executionSourceDigest": source.digest if source else None,
                    "inputDigest": digest_json(run.input_json),
                },
            )

    @staticmethod
    async def _finalize_recovery_state(
        session: AsyncSession, run: Run, status: str
    ) -> None:
        checkpoint_ids = select(ExecutionCheckpoint.id).where(
            ExecutionCheckpoint.run_id == run.id
        )
        await session.execute(
            update(RecoveryAttempt)
            .where(
                RecoveryAttempt.checkpoint_id.in_(checkpoint_ids),
                RecoveryAttempt.status == "active",
            )
            .values(status="succeeded" if status == "succeeded" else "failed")
        )
        observation = await session.scalar(
            select(FailureObservation)
            .where(
                FailureObservation.last_run_id == run.id,
                FailureObservation.last_checkpoint_id.is_not(None),
            )
            .order_by(FailureObservation.updated_at.desc())
            .limit(1)
        )
        if observation is not None:
            observation.recovered = status == "succeeded"
            observation.recovered_outcome_json = {
                "status": status,
                "outputDigest": (
                    digest_json(run.output_json) if run.output_json is not None else None
                ),
                "error": run.error_json,
            }

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
    runtime_snapshot: dict[str, Any]


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
            recovery_checkpoint = await session.scalar(
                select(ExecutionCheckpoint).where(
                    ExecutionCheckpoint.recovery_node_run_id == node_run.id,
                    ExecutionCheckpoint.status == "running",
                )
            )
            if recovery_checkpoint is not None:
                # RecoveryDriver 与 checkpoint 共用该租约并负责 outcome certainty；
                # 标准节点接管不得抢先把 Recovery NodeRun 改成普通 failed。
                continue
            run = await session.get(Run, node_run.run_id)
            if not run:
                continue
            source = await load_execution_source(session, run)
            if not source:
                continue
            definition = WorkflowDefinition.model_validate(source.definition_json)
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
            source = await load_execution_source(session, run)
            if source is None:
                continue
            definition = WorkflowDefinition.model_validate(source.definition_json)
            max_parallel = definition.spec.policies.max_parallel_nodes
            # 将并发配额检查折叠进领取 UPDATE：running 计数与所有权 CAS 是同一条
            # 原子语句。SQLite 串行化写入，两个 Worker 不会同时越过 Run 级上限。
            running_slots = (
                select(func.count())
                .select_from(NodeRun)
                .where(NodeRun.run_id == run.id, NodeRun.status == "running")
                .scalar_subquery()
            )
            from app.application.workflow_resources import conflicting_nodes

            conflicts = await conflicting_nodes(session, definition)
            conflict_slots = (
                select(func.count()).select_from(NodeRun).where(
                    NodeRun.run_id == run.id, NodeRun.status == "running",
                    NodeRun.node_id.in_(conflicts),
                ).scalar_subquery()
            )
            result = await session.execute(
                update(NodeRun)
                .where(
                    NodeRun.id == candidate.id,
                    NodeRun.status.in_(["ready", "retry_waiting"]),
                    NodeRun.attempt == candidate.attempt,
                    running_slots < max_parallel,
                    conflict_slots == 0 if candidate.node_id in conflicts else True,
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
                    for lock in source.validation_json.get("mcpLocks", [])
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
                runtime_snapshot=run.runtime_snapshot_json or {},
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
                    runtime_snapshot=claimed.runtime_snapshot,
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
        policy = error_policy(code)
        return NodeResult(
            status="cancelled" if code == "CANCELLED" else "failed",
            error={
                "code": policy.code,
                "message": message,
                "retryable": policy.retryable,
            },
        )

    async def _commit_result(self, claimed: ClaimedExecution, result: NodeResult) -> None:
        fixture = self._capture_replay_fixture(claimed, result)
        if fixture is not None:
            result.artifacts.append(fixture)
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
                error = (
                    persisted_error(
                        result.error.model_dump(),
                        technical_detail_ref=(
                            f"run:{claimed.run_id}:node:{claimed.node_run_id}"
                        ),
                    )
                    if result.error
                    else None
                )
                values.update(
                    status="waiting_user",
                    output_json=result.output,
                    error_json=error,
                )
                event_type = "approval.requested"
                event_payload = {
                    "nodeId": claimed.node_id,
                    "attempt": claimed.attempt,
                    "error": values["error_json"],
                }
            elif result.status == "cancelled":
                error = (
                    persisted_error(
                        result.error.model_dump(),
                        technical_detail_ref=(
                            f"run:{claimed.run_id}:node:{claimed.node_run_id}"
                        ),
                    )
                    if result.error
                    else None
                )
                values.update(
                    status="cancelled",
                    error_json=error,
                    finished_at=utcnow(),
                )
                event_type = "node.cancelled"
                event_payload = {"nodeId": claimed.node_id, "attempt": claimed.attempt}
            else:
                raw_error = (
                    result.error.model_dump()
                    if result.error
                    else {
                        "code": "EXECUTION_FAILED",
                        "message": "节点执行失败",
                        "retryable": False,
                    }
                )
                error = persisted_error(
                    raw_error,
                    technical_detail_ref=(
                        f"run:{claimed.run_id}:node:{claimed.node_run_id}"
                    ),
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
            if (
                claimed.node.type in {NodeType.LLM, NodeType.AGENT}
                and (
                    result.status == "succeeded"
                    or result.metrics.get("runtimeUsed")
                )
            ):
                from app.application.product_history import record_model_usage

                run_record = await session.get(Run, claimed.run_id)
                input_tokens = result.metrics.get("inputTokens")
                output_tokens = result.metrics.get("outputTokens")
                total_tokens = result.metrics.get("totalTokens")
                await record_model_usage(
                    session,
                    call_id=f"workflow-node:{claimed.node_run_id}",
                    attempt=claimed.attempt,
                    stage=(
                        "workflow_llm"
                        if claimed.node.type == NodeType.LLM
                        else "workflow_agent"
                    ),
                    turn_id=run_record.turn_id if run_record else None,
                    run_id=claimed.run_id,
                    provider=str(
                        result.metrics.get(
                            "provider",
                            "traex"
                            if claimed.node.type == NodeType.AGENT
                            else "openai-compatible",
                        )
                    ),
                    model=result.metrics.get("model"),
                    protocol=result.metrics.get("protocol"),
                    runtime_type=result.metrics.get("runtimeType"),
                    adapter_version=result.metrics.get("adapterVersion"),
                    adapter_digest=result.metrics.get("adapterDigest"),
                    runtime_used=(
                        str(result.metrics["runtimeUsed"])
                        if "runtimeUsed" in result.metrics
                        else (
                            "agent_runtime"
                            if claimed.node.type == NodeType.AGENT
                            else None
                        )
                    ),
                    runtime_digest=(
                        str(result.metrics["runtimeDigest"])
                        if "runtimeDigest" in result.metrics
                        else None
                    ),
                    fallback_reason=(
                        str(result.metrics["fallbackReason"])
                        if result.metrics.get("fallbackReason")
                        else None
                    ),
                    input_tokens=(
                        int(input_tokens) if isinstance(input_tokens, int) else None
                    ),
                    output_tokens=(
                        int(output_tokens) if isinstance(output_tokens, int) else None
                    ),
                    total_tokens=(
                        int(total_tokens) if isinstance(total_tokens, int) else None
                    ),
                    usage_complete=bool(
                        result.metrics.get(
                            "usageComplete",
                            total_tokens is not None
                            or (
                                input_tokens is not None
                                and output_tokens is not None
                            ),
                        )
                    ),
                    dispatched=bool(result.metrics.get("requestDispatched")),
                    completed=result.status == "succeeded",
                )
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
                if item["kind"] == "replay_fixture":
                    metadata = item.get("metadata", {})
                    session.add(
                        ReplayFixture(
                            id=str(uuid4()),
                            run_id=claimed.run_id,
                            node_run_id=claimed.node_run_id,
                            node_id=claimed.node_id,
                            artifact_id=item["id"],
                            source_digest=metadata["sourceDigest"],
                            input_digest=metadata["inputDigest"],
                            output_digest=metadata["outputDigest"],
                            capture_policy=metadata["capturePolicy"],
                            evidence_kind=metadata["evidenceKind"],
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

    @staticmethod
    def _capture_replay_fixture(
        claimed: ClaimedExecution, result: NodeResult
    ) -> dict[str, Any] | None:
        replayable_types = {
            NodeType.BROWSER,
            NodeType.MCP,
            NodeType.SKILLX,
            NodeType.AGENT,
            NodeType.LLM,
        }
        if (
            claimed.node.type not in replayable_types
            or claimed.node.side_effect
            or (
                result.status == "succeeded"
                and result.output is None
            )
            or (
                result.status == "failed"
                and result.error is None
            )
            or result.status not in {"succeeded", "failed"}
        ):
            return None
        if result.status == "succeeded":
            evidence = {
                "status": "succeeded",
                "output": redact(result.output),
            }
            evidence_value = evidence["output"]
            capture_policy = "success_redacted_schema_validated_64k"
            evidence_kind = "observed_output"
        else:
            from app.application.workflow_patch import evidence_kind_for_error

            evidence = {
                "status": "failed",
                "error": redact(result.error.model_dump()),
                "observedOutput": redact(result.output),
            }
            evidence_value = {
                "error": evidence["error"],
                "observedOutput": evidence["observedOutput"],
            }
            capture_policy = "failure_redacted_contract_evidence_64k"
            evidence_kind = evidence_kind_for_error(result.error.code)
            if (
                evidence_kind == "observed_contract_output"
                and not isinstance(evidence["observedOutput"], dict)
            ):
                evidence_kind = "unsupported"
        evidence["evidenceKind"] = evidence_kind
        encoded = json.dumps(
            evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > 64 * 1024:
            return None
        artifact = write_json_artifact(
            claimed.run_id,
            claimed.node_run_id,
            claimed.node_id,
            f"{claimed.node_id}-replay-fixture.json",
            "replay_fixture",
            evidence,
        )
        artifact["metadata"] = {
            **artifact.get("metadata", {}),
            "sourceDigest": digest_json(
                {
                    "type": claimed.node.type.value,
                    "config": claimed.node.config,
                }
            ),
            "inputDigest": digest_json(redact(claimed.inputs)),
            "outputDigest": digest_json(evidence_value),
            "capturePolicy": capture_policy,
            "evidenceKind": evidence_kind,
        }
        return artifact

    async def _schedule_retry(
        self,
        session: AsyncSession,
        claimed: ClaimedExecution,
        result: NodeResult,
    ) -> None:
        error = result.error
        policy = error_policy(error.code) if error else None
        # 统一重试策略：有副作用且非幂等禁止自动重试，结果未知不重试。
        node_effect = "write" if claimed.node.side_effect else "none"
        outcome = "unknown" if (error and error.code == "UNKNOWN_OUTCOME") else None
        if (
            not error
            or policy is None
            or not policy.retryable
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
    artifacts = (
        await session.scalars(select(Artifact).where(Artifact.run_id == run_id))
    ).all()
    root = (get_settings().artifact_root / run_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging_root = (root / ".staging").resolve()
    recorded: set[str] = set()
    recovered: list[str] = []
    missing: list[str] = []
    corrupt: list[str] = []
    path_rejected: list[str] = []
    for item in artifacts:
        metadata = item.metadata_json or {}
        storage_key = metadata.get("storageKey") or item.uri.removeprefix("artifact://")
        try:
            target = safe_artifact_path(
                root, storage_key, forbidden_root=staging_root
            )
        except (ArtifactPathError, TypeError):
            path_rejected.append(item.id)
            continue
        recorded.add(str(target.relative_to(root)))
        if target.exists() and artifact_record_matches(target, item):
            continue
        if target.exists():
            corrupt.append(item.id)
            target.unlink(missing_ok=True)
        staging_key = metadata.get("stagingKey")
        if not isinstance(staging_key, dict):
            missing.append(item.id)
            continue
        node_run_id = staging_key.get("nodeRunId")
        filename = staging_key.get("file")
        try:
            source = safe_artifact_path(
                staging_root, f"{node_run_id}/{filename}"
            )
        except (ArtifactPathError, TypeError):
            path_rejected.append(item.id)
            continue
        if not source.exists():
            missing.append(item.id)
            continue
        if not artifact_record_matches(source, item):
            if item.id not in corrupt:
                corrupt.append(item.id)
            source.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        recovered.append(item.id)

    final_files: set[str] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if ".staging" in relative.parts:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            path_rejected.append(str(relative))
        elif resolved.is_file():
            final_files.add(str(relative))
    orphan_files = sorted(final_files - recorded)
    for name in orphan_files:
        safe_artifact_path(root, name, forbidden_root=staging_root).unlink(
            missing_ok=True
        )
    staging_removed: list[str] = []
    if staging_root.is_dir():
        for candidate in staging_root.rglob("*"):
            resolved = candidate.resolve()
            if not resolved.is_relative_to(staging_root):
                path_rejected.append(str(candidate.relative_to(root)))
            elif resolved.is_file():
                staging_removed.append(str(candidate.relative_to(root)))
                resolved.unlink(missing_ok=True)
    return {
        "recovered": recovered,
        "missing": missing,
        "corrupt": corrupt,
        "pathRejected": sorted(path_rejected),
        "orphanRemoved": sorted([*orphan_files, *staging_removed]),
        "orphanFilesRemoved": orphan_files,
        "orphanStagingRemoved": sorted(staging_removed),
    }


async def reconcile_conversation_artifacts(
    session: AsyncSession, conversation_run_id: str
) -> dict[str, list[str]]:
    artifacts = (
        await session.scalars(
            select(Artifact).where(
                Artifact.conversation_run_id == conversation_run_id
            )
        )
    ).all()
    root = (
        get_settings().artifact_root
        / "conversation-agents"
        / conversation_run_id
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging_root = (root / ".staging").resolve()
    recorded: set[str] = set()
    recovered: list[str] = []
    missing: list[str] = []
    corrupt: list[str] = []
    path_rejected: list[str] = []

    for item in artifacts:
        metadata = item.metadata_json or {}
        storage_key = metadata.get("storageKey")
        if not isinstance(storage_key, str):
            missing.append(item.id)
            continue
        try:
            target = safe_artifact_path(
                root, storage_key, forbidden_root=staging_root
            )
        except ArtifactPathError:
            path_rejected.append(item.id)
            continue
        recorded.add(str(target.relative_to(root)))
        if target.exists():
            if artifact_record_matches(target, item):
                continue
            corrupt.append(item.id)
            target.unlink(missing_ok=True)
        staging_key = metadata.get("stagingKey")
        if not isinstance(staging_key, str):
            missing.append(item.id)
            continue
        try:
            source = safe_artifact_path(
                staging_root, staging_key.removeprefix(".staging/")
            )
        except ArtifactPathError:
            path_rejected.append(item.id)
            continue
        if not source.exists():
            missing.append(item.id)
            continue
        if not artifact_record_matches(source, item):
            if item.id not in corrupt:
                corrupt.append(item.id)
            source.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        recovered.append(item.id)
    final_files: set[str] = set()
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if ".staging" in relative.parts:
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            path_rejected.append(str(relative))
            continue
        if resolved.is_file():
            final_files.add(str(relative))
    orphan_files = sorted(final_files - recorded)
    for name in orphan_files:
        target = (root / name).resolve()
        if target != root and target.is_relative_to(root):
            target.unlink(missing_ok=True)
    staging_removed: list[str] = []
    if staging_root.is_dir():
        for candidate in staging_root.rglob("*"):
            resolved = candidate.resolve()
            if not resolved.is_relative_to(staging_root):
                path_rejected.append(str(candidate.relative_to(root)))
                continue
            if resolved.is_file():
                staging_removed.append(str(candidate.relative_to(root)))
                resolved.unlink(missing_ok=True)
    orphan_removed = sorted([*orphan_files, *staging_removed])
    return {
        "recovered": recovered,
        "missing": missing,
        "corrupt": corrupt,
        "pathRejected": sorted(path_rejected),
        "orphanRemoved": orphan_removed,
        "orphanFilesRemoved": orphan_files,
        "orphanStagingRemoved": sorted(staging_removed),
    }


async def reconcile_recent_runs(session: AsyncSession, *, limit: int = 50) -> dict[str, Any]:
    """扫描最近若干个 Run 并调用 Artifact reconcile，补落位提交后未完成的文件。

    Worker 启动与周期任务调用；限制批量大小，避免全表扫描。同时补落位 Conversation Agent
    轨迹（成功但落位未完成的 staging）。
    """

    run_ids = (
        await session.scalars(
            select(Run.id)
            .where(Run.status.in_(["succeeded", "failed", "cancelled"]))
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
    ).all()
    recovered = 0
    for run_id in run_ids:
        report = await reconcile_run_artifacts(session, run_id)
        recovered += len(report["recovered"])
    conversation_runs = (
        await session.scalars(
            select(ConversationRun.id)
            .where(
                ConversationRun.status.in_(
                    ["succeeded", "failed", "cancelled"]
                )
            )
            .order_by(ConversationRun.created_at.desc())
            .limit(limit)
        )
    ).all()
    for cr_id in conversation_runs:
        report = await reconcile_conversation_artifacts(session, cr_id)
        recovered += len(report["recovered"])
    return {
        "runsScanned": len(run_ids),
        "conversationRunsScanned": len(conversation_runs),
        "artifactsRecovered": recovered,
    }
