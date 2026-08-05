from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_run import AgentRunConfig, _append_event, enqueue_agent_run
from app.application.compiler import digest_json
from app.config import get_settings
from app.infrastructure.database import (
    Artifact,
    Conversation,
    ConversationRun,
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
        return await self.start_continuation(handle)

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
            completed = await self._load_completed_outputs(
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
            recovery_context = {
                "completed": completed,
                "completedActionLedger": completed_action_ledger,
                "remainingPlan": checkpoint.snapshot_json.get("remainingPlan"),
                "userAnswer": checkpoint.snapshot_json.get("userAnswer"),
            }
            prompt = (
                "你是受限 AgentRun Recovery Agent。只读取此 checkpoint 中已经验证的"
                " ToolResult，并从剩余计划继续；禁止读取全 Catalog，禁止重做已完成步骤，"
                "禁止扩大能力或副作用。\n"
                f"checkpoint={json.dumps(recovery_context, ensure_ascii=False)}\n"
                f"originalTask={source.input_json.get('message', '')}"
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
                "completedActionLedger": completed_action_ledger,
            }
            next_snapshot = {
                **checkpoint.snapshot_json,
                "continuationRunId": continuation.id,
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
        next_status = (
            "succeeded" if continuation.status == "succeeded" else "failed"
        )
        next_snapshot = {
            **checkpoint.snapshot_json,
            "continuationStatus": continuation.status,
            "continuationOutputDigest": digest_json(continuation.output_json),
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
        return True

    @staticmethod
    def _verify_checkpoint(
        checkpoint: TaskCheckpoint,
        source: ConversationRun,
    ) -> None:
        source_payload = {
            "kind": "conversation_run",
            "id": source.id,
            "input": source.input_json,
            "config": source.config_json or {},
            "trajectory": source.trajectory_json or {},
        }
        if digest_json(source_payload) != checkpoint.source_digest:
            raise TaskCheckpointError("checkpoint source digest 不一致")
        if digest_json(checkpoint.snapshot_json) != checkpoint.digest:
            raise TaskCheckpointError("checkpoint digest 不一致")
        if checkpoint.snapshot_json.get("outcomeCertainty") == "unknown":
            answer = checkpoint.snapshot_json.get("userAnswer")
            if not isinstance(answer, dict) or answer.get("continue") is not True:
                raise TaskCheckpointError("未知副作用结果必须由用户确认后继续")

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
            artifact = await session.get(Artifact, artifact_ref.get("id"))
            if (
                artifact is None
                or artifact.conversation_run_id != source_run_id
                or artifact.kind != "tool_result"
                or artifact.sha256 != artifact_ref.get("sha256")
            ):
                raise TaskCheckpointError("checkpoint ToolResult Artifact 关联错误")
            storage_key = artifact.metadata_json.get("storageKey")
            path = (
                get_settings().artifact_root
                / "conversation-agents"
                / source_run_id
                / str(storage_key)
            ).resolve()
            root = (
                get_settings().artifact_root
                / "conversation-agents"
                / source_run_id
            ).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise TaskCheckpointError(
                    "checkpoint ToolResult Artifact 路径越界或不存在"
                )
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != artifact.sha256:
                raise TaskCheckpointError("checkpoint ToolResult Artifact 已损坏")
            completed.append(json.loads(content))
        return completed
