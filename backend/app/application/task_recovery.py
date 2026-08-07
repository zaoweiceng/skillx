from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_plans import AgentPlanSnapshot
from app.application.agent_run import AgentRunConfig, _append_event, enqueue_agent_run
from app.application.artifact_integrity import (
    ArtifactPathError,
    artifact_record_matches,
    safe_artifact_path,
)
from app.application.compiler import digest_json
from app.config import get_settings
from app.infrastructure.database import (
    Artifact,
    Conversation,
    ConversationRun,
    Message,
    SessionLocal,
    TaskCheckpoint,
    utcnow,
)


class TaskCheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class ClaimedTaskCheckpoint:
    id: str
    owner: str
    generation: int
    token: str


class TaskCheckpointDriver:
    """Claim AgentRun checkpoints and create a constrained continuation run."""

    def __init__(
        self,
        worker_id: str = "task-recovery",
        *,
        lease_seconds: int | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds or get_settings().agent_lease_seconds

    async def poll_once(self) -> bool:
        async with SessionLocal() as session, session.begin():
            if await self._reconcile_continuation(session):
                return True
            handle = await self.claim(session)
        if handle is None:
            return False
        try:
            return await self.start_continuation(handle)
        except TaskCheckpointError as exc:
            await self._record_claim_error(handle, exc)
            return True

    async def claim(
        self, session: AsyncSession
    ) -> ClaimedTaskCheckpoint | None:
        now = utcnow()
        candidate = await session.scalar(
            select(TaskCheckpoint)
            .where(
                TaskCheckpoint.source_kind == "conversation_run",
                or_(
                    TaskCheckpoint.status == "pending",
                    (
                        (TaskCheckpoint.status == "running")
                        & (TaskCheckpoint.lease_expires_at < now)
                    ),
                ),
            )
            .order_by(TaskCheckpoint.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        chain_generation = int(
            candidate.snapshot_json.get("continuationGeneration", 0)
        )
        max_generations = get_settings().agent_max_continuation_generations
        if chain_generation >= max_generations:
            next_snapshot = {
                **candidate.snapshot_json,
                "budgetExhausted": {
                    "maxGenerations": max_generations,
                    "actualGenerations": chain_generation,
                },
            }
            exhausted = await session.execute(
                update(TaskCheckpoint)
                .where(
                    TaskCheckpoint.id == candidate.id,
                    TaskCheckpoint.status == candidate.status,
                    TaskCheckpoint.claim_generation
                    == candidate.claim_generation,
                )
                .values(
                    status="terminal",
                    snapshot_json=next_snapshot,
                    digest=digest_json(next_snapshot),
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            if exhausted.rowcount == 1:
                await _append_event(
                    session,
                    candidate.source_id,
                    "agent.handoff.budget_exhausted",
                    {
                        "taskCheckpointId": candidate.id,
                        "continuationGeneration": chain_generation,
                        "maxGenerations": max_generations,
                    },
                )
            return None
        token = str(uuid4())
        generation = candidate.claim_generation + 1
        claimed = await session.execute(
            update(TaskCheckpoint)
            .where(
                TaskCheckpoint.id == candidate.id,
                TaskCheckpoint.status == candidate.status,
                TaskCheckpoint.claim_generation == candidate.claim_generation,
            )
            .values(
                status="running",
                lease_owner=self.worker_id,
                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                claim_generation=generation,
                claim_token=token,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            return None
        return ClaimedTaskCheckpoint(
            id=candidate.id,
            owner=self.worker_id,
            generation=generation,
            token=token,
        )

    async def start_continuation(
        self, handle: ClaimedTaskCheckpoint
    ) -> bool:
        async with SessionLocal() as session, session.begin():
            checkpoint = await session.get(TaskCheckpoint, handle.id)
            if (
                checkpoint is None
                or checkpoint.status != "running"
                or checkpoint.lease_owner != handle.owner
                or checkpoint.claim_generation != handle.generation
                or checkpoint.claim_token != handle.token
            ):
                return False
            source = await session.get(ConversationRun, checkpoint.source_id)
            if source is None:
                raise TaskCheckpointError("checkpoint source ConversationRun 不存在")
            self._verify_checkpoint(checkpoint, source)
            plan_snapshot = await self._load_plan_snapshot(
                session, checkpoint, source.id
            )
            completed = await self._load_completed_outputs(
                session, checkpoint, source.id
            )
            failed = await self._load_failed_output(
                session, checkpoint, source.id
            )
            completed_by_signature = {
                str(item.get("actionSignature")): item
                for item in completed
                if item.get("actionSignature")
            }
            completed_action_ledger = []
            for item in checkpoint.snapshot_json.get(
                "completedActionLedger", []
            ):
                saved = completed_by_signature.get(str(item.get("signature")))
                completed_action_ledger.append(
                    {
                        **item,
                        **(
                            {"result": saved.get("output")}
                            if isinstance(saved, dict)
                            and isinstance(saved.get("output"), dict)
                            else {}
                        ),
                    }
                )
            conversation = await session.get(Conversation, source.conversation_id)
            if conversation is None:
                raise TaskCheckpointError("checkpoint conversation 不存在")
            source_config = AgentRunConfig.from_dict(source.config_json or {})
            allowed = tuple(
                item
                for item in source_config.allowed_capabilities
                if item in set(checkpoint.snapshot_json.get("locks", []))
            )
            config = AgentRunConfig(
                output_schema=source_config.output_schema,
                max_steps=max(1, source_config.max_steps - len(completed)),
                max_total_tokens=source_config.max_total_tokens,
                allowed_capabilities=allowed,
                max_duration_seconds=source_config.max_duration_seconds,
            )
            source_generation = int(
                checkpoint.snapshot_json.get("continuationGeneration", 0)
            )
            max_generations = get_settings().agent_max_continuation_generations
            if source_generation >= max_generations:
                raise TaskCheckpointError(
                    "continuation generation budget 已耗尽"
                )
            step_states = dict(
                checkpoint.snapshot_json.get("stepStates", {})
            )
            step_attempts = dict(
                checkpoint.snapshot_json.get("stepAttempts", {})
            )
            remaining_step_ids = [
                str(item.get("id"))
                for item in (
                    checkpoint.snapshot_json.get("remainingPlan", {}).get(
                        "steps", []
                    )
                )
                if item.get("id")
            ]
            step_outputs = {
                str(item["planStepId"]): dict(item["output"])
                for item in completed
                if item.get("planStepId")
                and isinstance(item.get("output"), dict)
            }
            artifact_outputs = {
                str(item["artifactId"]): item
                for item in completed
                if item.get("artifactId")
            }
            replan_budget = dict(
                checkpoint.snapshot_json.get("replanBudget") or {}
            )
            max_revisions = int(replan_budget.get("maxRevisions", 1))
            used_revisions = int(replan_budget.get("usedRevisions", 0))
            remaining_revisions = int(
                replan_budget.get("remainingRevisions", 1)
            )
            if (
                min(max_revisions, used_revisions, remaining_revisions) < 0
                or used_revisions + remaining_revisions != max_revisions
            ):
                raise TaskCheckpointError(
                    "checkpoint replan revision budget 不一致"
                )
            expected_plan_contract = {
                "planSnapshot": plan_snapshot,
                "planDigest": (
                    checkpoint.snapshot_json.get("planSnapshot") or {}
                ).get("digest"),
                "revision": (
                    checkpoint.snapshot_json.get("planSnapshot") or {}
                ).get("revision"),
                "allowedRemainingStepIds": remaining_step_ids,
                "sourceCheckpointId": checkpoint.id,
                "stepStates": step_states,
                "stepAttempts": step_attempts,
                "stepStateDigest": digest_json(step_states),
                "originalInput": {
                    "message": source.input_json.get("message", "")
                },
                "stepOutputs": step_outputs,
                "artifactOutputs": artifact_outputs,
                "continuationGeneration": source_generation + 1,
                "maxRevisions": max_revisions,
                "usedRevisions": used_revisions,
                "remainingRevisions": remaining_revisions,
                "legacyReplan": plan_snapshot is None,
            }
            recovery_context = {
                "schemaVersion": "1",
                "originalInput": source.input_json.get("message", ""),
                "reportedError": checkpoint.snapshot_json.get("reportedError"),
                "effectiveError": checkpoint.snapshot_json.get("effectiveError"),
                "completedOutputs": completed,
                "completedPlanSteps": checkpoint.snapshot_json.get(
                    "completedSteps", []
                ),
                "failedOutput": failed,
                "completedActionLedger": completed_action_ledger,
                "remainingPlan": checkpoint.snapshot_json.get("remainingPlan"),
                "sourcePlan": plan_snapshot,
                "sourcePlanDigest": expected_plan_contract["planDigest"],
                "sourcePlanRevision": expected_plan_contract["revision"],
                "sourceCheckpointId": checkpoint.id,
                "stepStates": step_states,
                "stepAttempts": step_attempts,
                "stepStateDigest": expected_plan_contract["stepStateDigest"],
                "capabilityLocks": list(allowed),
                "budget": {
                    "attempt": handle.generation,
                    "generation": source_generation + 1,
                    "maxGenerations": max_generations,
                    "maxSteps": config.max_steps,
                    "maxTotalTokens": config.max_total_tokens,
                    "maxRevisions": max_revisions,
                    "usedRevisions": used_revisions,
                    "remainingRevisions": remaining_revisions,
                },
                "userAnswer": checkpoint.snapshot_json.get("userAnswer"),
            }
            prompt = (
                "你是受限 AgentRun Recovery Agent。只读取此 checkpoint 中已经验证的"
                " ToolResult，并从剩余计划继续；禁止读取全 Catalog，禁止重做已完成步骤，"
                "禁止扩大能力或副作用。\n"
                "continuationRequest="
                f"{json.dumps(recovery_context, ensure_ascii=False)}"
            )
            continuation = await enqueue_agent_run(
                session,
                conversation,
                str(source.input_json.get("message", "")),
                config=config,
                prompt=prompt,
            )
            continuation.input_json = {
                **continuation.input_json,
                "continuationOfCheckpointId": checkpoint.id,
                "continuationOfConversationRunId": source.id,
                "rootSourceConversationRunId": source.input_json.get(
                    "rootSourceConversationRunId", source.id
                ),
                "completedActionLedger": completed_action_ledger,
                "continuationGeneration": source_generation + 1,
                "sourcePlanSnapshot": checkpoint.snapshot_json.get(
                    "planSnapshot"
                ),
                "expectedPlanContract": expected_plan_contract,
            }
            continuation_message = Message(
                id=str(uuid4()),
                conversation_id=conversation.id,
                role="assistant",
                content_json={
                    "type": "agent_run",
                    "state": continuation.status,
                    "conversationRunId": continuation.id,
                    "runType": "agent_run",
                    "text": (
                        f"已创建第 {source_generation + 1} 代失败接力，"
                        "将从已冻结的剩余计划继续。"
                    ),
                    "sourceRunId": source.id,
                    "rootSourceRunId": source.input_json.get(
                        "rootSourceConversationRunId", source.id
                    ),
                    "continuationOfConversationRunId": source.id,
                    "taskCheckpointId": checkpoint.id,
                    "continuationGeneration": source_generation + 1,
                },
            )
            session.add(continuation_message)
            next_snapshot = {
                **checkpoint.snapshot_json,
                "continuationRunId": continuation.id,
                "continuationGeneration": source_generation + 1,
                "continuationStatus": continuation.status,
            }
            claimed = await session.execute(
                update(TaskCheckpoint)
                .where(
                    TaskCheckpoint.id == handle.id,
                    TaskCheckpoint.status == "running",
                    TaskCheckpoint.lease_owner == handle.owner,
                    TaskCheckpoint.claim_generation == handle.generation,
                    TaskCheckpoint.claim_token == handle.token,
                )
                .values(
                    status="continuing",
                    snapshot_json=next_snapshot,
                    digest=digest_json(next_snapshot),
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                await session.delete(continuation_message)
                await session.delete(continuation)
                return False
            await _append_event(
                session,
                source.id,
                "agent.handoff.started",
                {
                    "taskCheckpointId": checkpoint.id,
                    "continuationRunId": continuation.id,
                    "claimGeneration": handle.generation,
                },
            )
            return True

    async def _record_claim_error(
        self,
        handle: ClaimedTaskCheckpoint,
        error: TaskCheckpointError,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            checkpoint = await session.get(TaskCheckpoint, handle.id)
            if (
                checkpoint is None
                or checkpoint.status != "running"
                or checkpoint.lease_owner != handle.owner
                or checkpoint.claim_generation != handle.generation
                or checkpoint.claim_token != handle.token
            ):
                return
            next_snapshot = {
                **checkpoint.snapshot_json,
                "continuationError": {
                    "code": "CONTINUATION_CONTRACT_INVALID",
                    "message": str(error),
                },
            }
            failed = await session.execute(
                update(TaskCheckpoint)
                .where(
                    TaskCheckpoint.id == handle.id,
                    TaskCheckpoint.status == "running",
                    TaskCheckpoint.lease_owner == handle.owner,
                    TaskCheckpoint.claim_generation == handle.generation,
                    TaskCheckpoint.claim_token == handle.token,
                )
                .values(
                    status="terminal",
                    snapshot_json=next_snapshot,
                    digest=digest_json(next_snapshot),
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                )
                .execution_options(synchronize_session=False)
            )
            if failed.rowcount == 1:
                await _append_event(
                    session,
                    checkpoint.source_id,
                    "agent.handoff.rejected",
                    {
                        "taskCheckpointId": checkpoint.id,
                        "code": "CONTINUATION_CONTRACT_INVALID",
                        "message": str(error),
                    },
                )

    async def _reconcile_continuation(self, session: AsyncSession) -> bool:
        checkpoint = await session.scalar(
            select(TaskCheckpoint)
            .where(TaskCheckpoint.status == "continuing")
            .order_by(TaskCheckpoint.updated_at)
            .limit(1)
        )
        if checkpoint is None:
            return False
        continuation_id = checkpoint.snapshot_json.get("continuationRunId")
        continuation = await session.get(ConversationRun, continuation_id)
        if continuation is None or continuation.status not in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            return False
        child_checkpoint = None
        if continuation.status == "failed":
            child_checkpoint = await session.scalar(
                select(TaskCheckpoint).where(
                    TaskCheckpoint.source_kind == "conversation_run",
                    TaskCheckpoint.source_id == continuation.id,
                )
            )
        next_status = (
            "succeeded" if continuation.status == "succeeded" else "failed"
        )
        continuation_error = (
            continuation.error_json
            if continuation.status == "failed"
            else {
                "code": "CONTINUATION_CANCELLED",
                "message": "失败接力已取消",
                "retryable": False,
            }
            if continuation.status == "cancelled"
            else None
        )
        next_snapshot = {
            **checkpoint.snapshot_json,
            "continuationStatus": continuation.status,
            "continuationOutputDigest": digest_json(continuation.output_json),
            **(
                {"continuationError": continuation_error}
                if continuation_error is not None
                else {}
            ),
            **(
                {"childCheckpointId": child_checkpoint.id}
                if child_checkpoint is not None
                else {}
            ),
        }
        reconciled = await session.execute(
            update(TaskCheckpoint)
            .where(
                TaskCheckpoint.id == checkpoint.id,
                TaskCheckpoint.status == "continuing",
                TaskCheckpoint.claim_generation == checkpoint.claim_generation,
                TaskCheckpoint.claim_token == checkpoint.claim_token,
            )
            .values(
                status=next_status,
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                snapshot_json=next_snapshot,
                digest=digest_json(next_snapshot),
            )
            .execution_options(synchronize_session=False)
        )
        if reconciled.rowcount != 1:
            return False
        await _append_event(
            session,
            checkpoint.source_id,
            (
                "agent.handoff.succeeded"
                if continuation.status == "succeeded"
                else "agent.handoff.failed"
            ),
            {
                "taskCheckpointId": checkpoint.id,
                "continuationRunId": continuation.id,
            },
        )
        if child_checkpoint is not None:
            next_generation = int(
                continuation.input_json.get(
                    "continuationGeneration", 0
                )
            )
            child_snapshot = {
                **child_checkpoint.snapshot_json,
                "continuationGeneration": next_generation,
                "sourceCheckpointId": checkpoint.id,
                "rootSourceRunId": checkpoint.snapshot_json.get(
                    "rootSourceRunId", checkpoint.source_id
                ),
            }
            child_checkpoint.snapshot_json = child_snapshot
            child_checkpoint.digest = digest_json(child_snapshot)
        return True

    @staticmethod
    def _verify_checkpoint(
        checkpoint: TaskCheckpoint,
        source: ConversationRun,
    ) -> None:
        source_payload = checkpoint.snapshot_json.get("sourceEvidence")
        if not isinstance(source_payload, dict):
            raise TaskCheckpointError(
                "旧 checkpoint 缺少 sourceEvidence，必须 replan"
            )
        if (
            source_payload.get("kind") != "conversation_run"
            or source_payload.get("id") != source.id
            or source_payload.get("input") != source.input_json
            or source_payload.get("config") != (source.config_json or {})
        ):
            raise TaskCheckpointError("checkpoint source 事实不一致")
        if digest_json(source_payload) != checkpoint.source_digest:
            raise TaskCheckpointError("checkpoint source digest 不一致")
        if digest_json(checkpoint.snapshot_json) != checkpoint.digest:
            raise TaskCheckpointError("checkpoint digest 不一致")
        if checkpoint.snapshot_json.get("outcomeCertainty") == "unknown":
            answer = checkpoint.snapshot_json.get("userAnswer")
            if not isinstance(answer, dict) or answer.get("continue") is not True:
                raise TaskCheckpointError("未知副作用结果必须由用户确认后继续")

    @staticmethod
    async def _load_plan_snapshot(
        session: AsyncSession,
        checkpoint: TaskCheckpoint,
        source_run_id: str,
    ) -> dict[str, Any] | None:
        plan_ref = checkpoint.snapshot_json.get("planSnapshot")
        mode = (
            checkpoint.snapshot_json.get("remainingPlan") or {}
        ).get("mode")
        if not isinstance(plan_ref, dict):
            if mode == "replan_required":
                answer = checkpoint.snapshot_json.get("userAnswer")
                if isinstance(answer, dict) and answer.get("continue") is True:
                    return None
                raise TaskCheckpointError(
                    "checkpoint 标记为 replan_required，未获人工确认"
                )
            raise TaskCheckpointError("checkpoint 缺少 PlanSnapshot")
        artifact = await session.get(Artifact, plan_ref.get("id"))
        if (
            artifact is None
            or artifact.conversation_run_id != source_run_id
            or artifact.kind != "agent_plan_snapshot"
            or artifact.sha256 != plan_ref.get("sha256")
            or artifact.size != plan_ref.get("bytes")
            or artifact.metadata_json.get("planDigest")
            != plan_ref.get("digest")
        ):
            raise TaskCheckpointError("checkpoint PlanSnapshot Artifact 关联错误")
        root = (
            get_settings().artifact_root
            / "conversation-agents"
            / source_run_id
        ).resolve()
        try:
            path = safe_artifact_path(
                root,
                artifact.metadata_json.get("storageKey"),
                forbidden_root=root / ".staging",
            )
        except ArtifactPathError as exc:
            raise TaskCheckpointError(
                "checkpoint PlanSnapshot Artifact 路径越界"
            ) from exc
        if not artifact_record_matches(path, artifact):
            raise TaskCheckpointError(
                "checkpoint PlanSnapshot Artifact 不存在或已损坏"
            )
        try:
            payload = json.loads(path.read_bytes())
            AgentPlanSnapshot.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise TaskCheckpointError(
                "checkpoint PlanSnapshot 内容非法"
            ) from exc
        if digest_json(payload) != plan_ref.get("digest"):
            raise TaskCheckpointError("checkpoint PlanSnapshot digest 不一致")
        return payload

    @staticmethod
    async def _load_completed_outputs(
        session: AsyncSession,
        checkpoint: TaskCheckpoint,
        source_run_id: str,
    ) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for step in checkpoint.snapshot_json.get("completedSteps", []):
            artifact_ref = step.get("artifact")
            if not isinstance(artifact_ref, dict):
                continue
            completed.append(
                {
                    **(
                        await TaskCheckpointDriver._load_artifact_envelope(
                            session, artifact_ref, source_run_id
                        )
                    ),
                    "artifactId": artifact_ref.get("id"),
                }
            )
        return completed

    @staticmethod
    async def _load_failed_output(
        session: AsyncSession,
        checkpoint: TaskCheckpoint,
        source_run_id: str,
    ) -> dict[str, Any] | None:
        artifact_ref = checkpoint.snapshot_json.get("failedAction", {}).get(
            "artifact"
        )
        if not isinstance(artifact_ref, dict):
            return checkpoint.snapshot_json.get("failedAction")
        envelope = await TaskCheckpointDriver._load_artifact_envelope(
            session, artifact_ref, source_run_id
        )
        return {
            **checkpoint.snapshot_json.get("failedAction", {}),
            "toolResult": envelope,
        }

    @staticmethod
    async def _load_artifact_envelope(
        session: AsyncSession,
        artifact_ref: dict[str, Any],
        source_run_id: str,
    ) -> dict[str, Any]:
        artifact = await session.get(Artifact, artifact_ref.get("id"))
        if (
            artifact is None
            or artifact.conversation_run_id != source_run_id
            or artifact.kind != "tool_result"
            or artifact.sha256 != artifact_ref.get("sha256")
            or artifact.metadata_json.get("evidenceDigest")
            != artifact_ref.get("evidenceDigest")
        ):
            raise TaskCheckpointError("checkpoint ToolResult Artifact 关联错误")
        root = (
            get_settings().artifact_root
            / "conversation-agents"
            / source_run_id
        ).resolve()
        try:
            path = safe_artifact_path(
                root,
                artifact.metadata_json.get("storageKey"),
                forbidden_root=root / ".staging",
            )
        except ArtifactPathError as exc:
            raise TaskCheckpointError(
                "checkpoint ToolResult Artifact 路径越界"
            ) from exc
        if not artifact_record_matches(path, artifact):
            raise TaskCheckpointError(
                "checkpoint ToolResult Artifact 不存在或已损坏"
            )
        try:
            return json.loads(path.read_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TaskCheckpointError(
                "checkpoint ToolResult Artifact 内容非法"
            ) from exc
