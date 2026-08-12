from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    ExpectedPlanContract,
    resolve_effective_capabilities,
    resolve_local_demo_capabilities,
)
from app.application.artifact_integrity import (
    ArtifactPathError,
    artifact_record_matches,
    safe_artifact_path,
)
from app.application.compiler import digest_json
from app.application.error_policy import error_policy, persisted_error, public_error
from app.application.product_history import record_model_usage
from app.application.runtime_config import agent_runtime_config
from app.config import get_settings
from app.infrastructure.database import (
    Artifact,
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
    TaskCheckpoint,
    rfc3339,
    utcnow,
)
from app.runtime.adapters import TraeXSubprocessAdapter
from app.runtime.traex_builtin import TraeXBuiltinAdapter

FINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class AgentRunError(Exception):
    """AgentRunService 归一化过程中的确定性失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        failed_call_id: str | None = None,
        protocol_violation: str | None = None,
        reported_error: dict[str, Any] | None = None,
        request_dispatched: bool = False,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        usage_complete: bool = False,
    ) -> None:
        policy = error_policy(code)
        self.code = policy.code
        self.message = message
        self.retryable = policy.retryable
        self.failed_call_id = failed_call_id
        self.protocol_violation = protocol_violation
        self.reported_error = reported_error
        self.request_dispatched = request_dispatched
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.usage_complete = usage_complete
        self.effective_error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        super().__init__(message)


@dataclass
class AgentRunConfig:
    """一次受限通用 Agent 运行的配置。

    ``allowed_capabilities`` 是请求方**期望**的 Tool 集合；真正生效的权限是它与
    Host policy 的交集（见 :func:`resolve_effective_capabilities`）。步数/Token/时长
    上限由 Host 独立强制，请求只能收紧、不能放宽。
    """

    output_schema: dict[str, Any]
    max_steps: int = 6
    max_total_tokens: int = 30000
    max_input_tokens: int = 40000
    max_output_tokens: int = 8000
    max_billable_tokens: int = 48000
    allowed_capabilities: tuple[str, ...] = ()
    max_duration_seconds: int = 120

    @staticmethod
    def default() -> AgentRunConfig:
        settings = get_settings()
        allowed_capabilities, _ = resolve_local_demo_capabilities(None)
        return AgentRunConfig(
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            },
            max_steps=settings.agent_max_steps,
            max_total_tokens=settings.agent_max_total_tokens,
            max_input_tokens=settings.agent_max_input_tokens,
            max_output_tokens=settings.agent_max_output_tokens,
            max_billable_tokens=settings.agent_max_billable_tokens,
            allowed_capabilities=allowed_capabilities,
            max_duration_seconds=settings.agent_max_duration_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outputSchema": self.output_schema,
            "maxSteps": self.max_steps,
            "maxTotalTokens": self.max_total_tokens,
            "maxInputTokens": self.max_input_tokens,
            "maxOutputTokens": self.max_output_tokens,
            "maxBillableTokens": self.max_billable_tokens,
            "allowedCapabilities": list(self.allowed_capabilities),
            "maxDurationSeconds": self.max_duration_seconds,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> AgentRunConfig:
        default = AgentRunConfig.default()
        return AgentRunConfig(
            output_schema=data.get("outputSchema", default.output_schema),
            max_steps=int(data.get("maxSteps", default.max_steps)),
            max_total_tokens=int(data.get("maxTotalTokens", default.max_total_tokens)),
            max_input_tokens=int(data.get("maxInputTokens", default.max_input_tokens)),
            max_output_tokens=int(data.get("maxOutputTokens", default.max_output_tokens)),
            max_billable_tokens=int(
                data.get("maxBillableTokens", default.max_billable_tokens)
            ),
            allowed_capabilities=tuple(data.get("allowedCapabilities", [])),
            max_duration_seconds=int(
                data.get("maxDurationSeconds", default.max_duration_seconds)
            ),
        )


async def _append_event(
    session: AsyncSession,
    run_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """追加规范化事件；seq 由数据库原子递增，不依赖进程内局部变量。"""

    seq = await session.scalar(
        update(ConversationRun)
        .where(ConversationRun.id == run_id)
        .values(next_event_seq=ConversationRun.next_event_seq + 1)
        .returning(ConversationRun.next_event_seq)
    )
    if seq is None:
        raise LookupError("conversation run not found")
    safe_payload = dict(payload)
    if isinstance(safe_payload.get("error"), dict):
        safe_payload["error"] = public_error(safe_payload["error"])
    session.add(
        ConversationRunEvent(
            id=str(uuid4()),
            conversation_run_id=run_id,
            seq=seq,
            type=event_type,
            payload_json=safe_payload,
        )
    )
    await session.flush()


async def enqueue_agent_run(
    session: AsyncSession,
    conversation: Conversation,
    content: str,
    *,
    config: AgentRunConfig | None = None,
    prompt: str | None = None,
    turn_id: str | None = None,
) -> ConversationRun:
    """先持久化、后执行：在短事务内创建 ``queued`` run 与 created 事件并返回 handle。

    运行的存在、状态与事件由此不再依赖 HTTP 连接或某个进程内存。真正的 TraeX 执行由
    独立 :class:`AgentRunWorker` 领取租约后完成。
    """

    config = config or AgentRunConfig.default()
    now = utcnow()
    conversation_run = ConversationRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        turn_id=turn_id,
        status="queued",
        run_type="agent_run",
        input_json={"message": content, "prompt": prompt if prompt is not None else content},
        output_json={},
        trajectory_json={},
        config_json=config.to_dict(),
        queued_at=now,
        attempt=1,
        next_event_seq=0,
    )
    session.add(conversation_run)
    await session.flush()
    await _append_event(
        session,
        conversation_run.id,
        "agent.run.created",
        {
            "conversationRunId": conversation_run.id,
            "input": {"message": content},
            "capabilityPolicy": "local_demo_read_only",
            "allowedCapabilities": list(config.allowed_capabilities),
        },
    )
    return conversation_run


async def request_cancellation(session: AsyncSession, run_id: str) -> ConversationRun:
    """请求取消一次 Agent 运行。

    - queued（未被领取）：直接置为 ``cancelled``，Worker 不会再领取；
    - running：记录 ``cancellation_requested_at``，由持有租约的 Worker 传播到子进程。
    """

    run = await session.get(ConversationRun, run_id)
    if run is None:
        raise LookupError("conversation run not found")
    if run.status in FINAL_STATES:
        return run
    run.cancellation_requested_at = utcnow()
    if run.status == "queued":
        run.status = "cancelled"
        run.finished_at = utcnow()
        await _append_event(
            session,
            run.id,
            "agent.run.cancelled",
            {"conversationRunId": run.id, "reason": "取消于排队阶段"},
        )
    else:
        await _append_event(
            session,
            run.id,
            "agent.run.cancellation_requested",
            {"conversationRunId": run.id},
        )
    return run


class AgentRunWorker:
    """领取并执行持久化的通用 Agent 长任务。

    与工作流 Worker 对称：通过租约领取 ``queued`` run，启动受限 TraeX，逐条提交
    归一化事件与心跳；独立强制权限交集、步数/Token/时长上限与事件契约；观察取消请求；
    对过期租约的 running 运行按 ``UNKNOWN_OUTCOME`` 恢复为唯一终态，绝不盲目重跑。
    """

    def __init__(
        self,
        worker_id: str = "agent-worker",
        *,
        adapter: TraeXSubprocessAdapter | TraeXBuiltinAdapter | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        self.worker_id = worker_id
        self._adapter = adapter
        self.lease_seconds = lease_seconds or get_settings().agent_lease_seconds

    async def poll_once(self) -> bool:
        """恢复过期租约并领取执行一次；返回是否处理了任务。"""

        async with SessionLocal() as session, session.begin():
            await self.recover_expired(session)
        async with SessionLocal() as session, session.begin():
            run_id = await self.claim(session)
        if run_id is None:
            return False
        await self.execute(run_id)
        return True

    async def recover_expired(self, session: AsyncSession) -> int:
        """把过期租约的 running 运行恢复为 UNKNOWN_OUTCOME 终态。

        Agent 可能已经产生 Tool 副作用而结果未知，盲目重跑不安全，因此形成唯一失败
        终态而不是再起一个 TraeX 进程。
        """

        now = utcnow()
        expired = (
            await session.scalars(
                select(ConversationRun).where(
                    ConversationRun.run_type == "agent_run",
                    ConversationRun.status == "running",
                    ConversationRun.lease_expires_at < now,
                )
            )
        ).all()
        recovered = 0
        for run in expired:
            error = {
                "code": "UNKNOWN_OUTCOME",
                "message": "Worker 租约过期，无法确认 Agent 副作用是否已完成",
                "retryable": False,
            }
            checkpoint = await self._create_task_checkpoint(
                session, run, error, status="awaiting_user"
            )
            error["taskCheckpointId"] = checkpoint.id
            result = await session.execute(
                update(ConversationRun)
                .where(
                    ConversationRun.id == run.id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == run.lease_owner,
                    ConversationRun.claim_generation == run.claim_generation,
                    ConversationRun.claim_token == run.claim_token,
                )
                .values(
                    status="failed",
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                    error_json=error,
                    finished_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                await _append_event(
                    session,
                    run.id,
                    "agent.run.failed",
                    {"conversationRunId": run.id, "error": error},
                )
                recovered += 1
            else:
                await session.delete(checkpoint)
        return recovered

    async def claim(self, session: AsyncSession) -> str | None:
        now = utcnow()
        candidate = await session.scalar(
            select(ConversationRun)
            .where(
                ConversationRun.run_type == "agent_run",
                ConversationRun.status == "queued",
            )
            .order_by(ConversationRun.created_at)
            .limit(1)
        )
        if candidate is None:
            return None
        result = await session.execute(
            update(ConversationRun)
            .where(
                ConversationRun.id == candidate.id,
                ConversationRun.status == "queued",
                ConversationRun.claim_generation == candidate.claim_generation,
            )
            .values(
                status="running",
                lease_owner=self.worker_id,
                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                heartbeat_at=now,
                started_at=now,
                claim_generation=candidate.claim_generation + 1,
                claim_token=str(uuid4()),
            )
        )
        if result.rowcount != 1:
            return None
        await _append_event(
            session,
            candidate.id,
            "agent.run.started",
            {"conversationRunId": candidate.id, "worker": self.worker_id},
        )
        return candidate.id

    async def execute(self, run_id: str) -> None:
        async with SessionLocal() as session:
            run = await session.get(ConversationRun, run_id)
            if run is None or run.status != "running":
                return
            config = AgentRunConfig.from_dict(run.config_json or {})
            content = run.input_json.get("message", "")
            prompt = run.input_json.get("prompt", content)
            claim_generation = run.claim_generation
            claim_token = run.claim_token

        # denied 能力在 Host 内 fail closed（PERMISSION_DENIED）；这里仅提前记录审计事件。
        _, denied = resolve_effective_capabilities(config.allowed_capabilities)
        if denied:
            await self._append_committed(
                run_id,
                "agent.capabilities.denied",
                {"requested": list(config.allowed_capabilities), "denied": denied},
                claim_generation,
                claim_token,
            )

        # 后台 watcher 负责租约心跳与取消/丢失检测，主执行交给统一 AgentHost。
        signal: dict[str, Any] = {}
        stop_watch = asyncio.Event()
        watcher = asyncio.create_task(
            self._watch(
                run_id,
                signal,
                stop_watch,
                claim_generation,
                claim_token,
            )
        )

        host = AgentHost(adapter=self._adapter)

        async def event_sink(runtime: dict[str, Any], audit: dict[str, Any]) -> None:
            if runtime["type"] == "agent.plan" and isinstance(
                runtime["payload"].get("steps"), list
            ):
                await self._persist_plan_snapshot_artifact(
                    run_id,
                    runtime["payload"],
                    claim_generation,
                    claim_token,
                )
            elif runtime["type"] == "tool.result":
                await self._persist_tool_result_artifact(
                    run_id,
                    runtime["payload"],
                    claim_generation,
                    claim_token,
                )
            # 审计事件只保存摘要；可恢复值由独立、有界 Artifact 承载。
            await self._append_committed(
                run_id,
                audit["type"],
                audit["payload"],
                claim_generation,
                claim_token,
            )

        async def cancellation() -> bool:
            return bool(signal)

        expected_plan_data = run.input_json.get("expectedPlanContract")
        expected_plan = (
            ExpectedPlanContract(
                plan_snapshot=expected_plan_data.get("planSnapshot"),
                plan_digest=expected_plan_data.get("planDigest"),
                revision=expected_plan_data.get("revision"),
                allowed_remaining_step_ids=tuple(
                    expected_plan_data.get("allowedRemainingStepIds", [])
                ),
                source_checkpoint_id=str(
                    expected_plan_data.get("sourceCheckpointId", "")
                ),
                step_states=dict(
                    expected_plan_data.get("stepStates", {})
                ),
                step_attempts=dict(
                    expected_plan_data.get("stepAttempts", {})
                ),
                step_state_digest=str(
                    expected_plan_data.get("stepStateDigest", "")
                ),
                original_input=dict(
                    expected_plan_data.get("originalInput", {})
                ),
                step_outputs=dict(
                    expected_plan_data.get("stepOutputs", {})
                ),
                artifact_outputs=dict(
                    expected_plan_data.get("artifactOutputs", {})
                ),
                continuation_generation=int(
                    expected_plan_data.get("continuationGeneration", 0)
                ),
                max_revisions=int(
                    expected_plan_data.get("maxRevisions", 1)
                ),
                used_revisions=int(
                    expected_plan_data.get("usedRevisions", 0)
                ),
                remaining_revisions=int(
                    expected_plan_data.get("remainingRevisions", 1)
                ),
                legacy_replan=bool(
                    expected_plan_data.get("legacyReplan", False)
                ),
            )
            if isinstance(expected_plan_data, dict)
            else None
        )
        request = AgentHostRequest(
            run_id=run_id,
            prompt=prompt,
            output_schema=config.output_schema,
            working_directory=get_settings().artifact_root / "conversation-agents" / run_id,
            requested_capabilities=config.allowed_capabilities,
            max_steps=config.max_steps,
            max_total_tokens=config.max_total_tokens,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_billable_tokens=config.max_billable_tokens,
            max_duration_seconds=config.max_duration_seconds,
            completed_actions=tuple(
                run.input_json.get("completedActionLedger", [])
            ),
            expected_plan=expected_plan,
        )

        terminal: AgentRunError | None = None
        result = None
        try:
            result = await host.execute(
                request, event_sink=event_sink, cancellation=cancellation
            )
        except AgentHostError as exc:
            terminal = AgentRunError(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                failed_call_id=exc.failed_call_id,
                protocol_violation=exc.protocol_violation,
                reported_error=exc.reported_error,
                request_dispatched=exc.request_dispatched,
                input_tokens=exc.input_tokens,
                output_tokens=exc.output_tokens,
                total_tokens=exc.total_tokens,
                usage_complete=exc.usage_complete,
            )
        except asyncio.CancelledError:
            terminal = None  # 由 signal 决定 cancelled/lost
        finally:
            stop_watch.set()
            await watcher

        # 取消/丢失租约优先于其它终态。
        if signal.get("kind") == "lost":
            return  # 已被恢复或另一个 Worker 接管，不再写入终态。
        if signal.get("kind") == "cancelled":
            await self._finalize_cancelled(
                run_id, claim_generation, claim_token
            )
            return
        if terminal is not None:
            await self._finalize_failure(
                run_id, terminal, claim_generation, claim_token
            )
            return
        if result is None:
            await self._finalize_failure(
                run_id,
                AgentRunError("EXECUTION_FAILED", "Agent 未产生结果"),
                claim_generation,
                claim_token,
            )
            return
        await self._finalize_success(
            run_id,
            config,
            prompt,
            result.output,
            result.actions,
            result.runtime_events,
            result.step_count,
            result.total_tokens,
            claim_generation,
            claim_token,
            usage_complete=result.usage_complete,
            request_dispatched=result.request_dispatched,
            accepted_plan_digest=result.accepted_plan_digest,
            accepted_plan_revision=result.accepted_plan_revision,
            plan_step_states=result.plan_step_states,
            plan_step_attempts=result.plan_step_attempts,
            max_revisions=result.max_revisions,
            used_revisions=result.used_revisions,
            remaining_revisions=result.remaining_revisions,
            provider=result.provider,
            model=result.model,
            protocol=result.protocol,
            runtime_type=result.runtime_type,
            adapter_version=result.adapter_version,
            adapter_digest=result.adapter_digest,
        )

    async def _watch(
        self,
        run_id: str,
        signal: dict[str, Any],
        stop: asyncio.Event,
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        """周期性刷新租约并观察取消请求；置位 signal 让 AgentHost 停止。"""

        interval = get_settings().agent_lease_seconds
        tick = max(0.2, min(0.5, interval / 3))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
                break
            except TimeoutError:
                pass
            if signal:
                break
            async with SessionLocal() as session, session.begin():
                run = await session.get(ConversationRun, run_id)
                if run is None:
                    signal["kind"] = "lost"
                    break
                if run.cancellation_requested_at is not None:
                    # 置位 signal，AgentHost 的 cancellation 回调会终止子进程。
                    signal["kind"] = "cancelled"
                    break
                result = await session.execute(
                    update(ConversationRun)
                    .where(
                        ConversationRun.id == run_id,
                        ConversationRun.status == "running",
                        ConversationRun.lease_owner == self.worker_id,
                        ConversationRun.claim_generation == claim_generation,
                        ConversationRun.claim_token == claim_token,
                    )
                    .values(
                        heartbeat_at=utcnow(),
                        lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds),
                    )
                )
                if result.rowcount != 1:
                    signal["kind"] = "lost"
                    break

    async def _append_committed(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            owned = await session.scalar(
                select(ConversationRun.id).where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
            )
            if owned is None:
                raise asyncio.CancelledError
            await _append_event(session, run_id, event_type, payload)

    async def _persist_tool_result_artifact(
        self,
        run_id: str,
        payload: dict[str, Any],
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        if payload.get("effect") != "none":
            return
        from app.application.security import redact

        envelope = {
            "callId": payload.get("callId"),
            "capabilityRef": payload.get("capabilityRef"),
            "status": payload.get("status"),
            "argumentsDigest": payload.get("argumentsDigest"),
            "output": redact(payload.get("output")),
            "error": redact(payload.get("error")),
            "evidenceDigest": payload.get("evidenceDigest"),
            "outputSchema": payload.get("outputSchema"),
            "actionSignature": payload.get("actionSignature"),
            "planStepId": payload.get("planStepId"),
            "planDigest": payload.get("planDigest"),
            "planRevision": payload.get("planRevision"),
            "stepAttempt": payload.get("stepAttempt"),
        }
        content = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(content) > 64 * 1024:
            return
        call_id = str(payload.get("callId", "unknown"))
        storage_key = (
            "tool-results/"
            + hashlib.sha256(call_id.encode()).hexdigest()[:24]
            + ".json"
        )
        token = claim_token or "unclaimed"
        staging_key = f".staging/{token}/{storage_key}"
        root = self._trajectory_dir(run_id)
        staging = (root / staging_key).resolve()
        if not staging.is_relative_to(root):
            raise AgentHostError(
                "EVIDENCE_CONFLICT", "ToolResult staging 路径越界"
            )
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(content)
        content_hash = hashlib.sha256(content).hexdigest()
        created = False
        async with SessionLocal() as session, session.begin():
            owned = await session.scalar(
                select(ConversationRun.id).where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
            )
            if owned is None:
                staging.unlink(missing_ok=True)
                raise asyncio.CancelledError
            existing = await session.scalar(
                select(Artifact).where(
                    Artifact.conversation_run_id == run_id,
                    Artifact.kind == "tool_result",
                    Artifact.name == f"{call_id}.json",
                )
            )
            if existing is not None:
                staging.unlink(missing_ok=True)
                if existing.sha256 != content_hash:
                    raise AgentHostError(
                        "EVIDENCE_CONFLICT",
                        f"duplicate call id {call_id} 的 ToolResult hash 不一致",
                    )
                return
            session.add(
                Artifact(
                    id=str(uuid4()),
                    run_id=None,
                    conversation_run_id=run_id,
                    kind="tool_result",
                    name=f"{call_id}.json",
                    uri=f"artifact://conversation-agents/{run_id}/{storage_key}",
                    sha256=content_hash,
                    size=len(content),
                    media_type="application/json",
                    metadata_json={
                        "callId": call_id,
                        "capabilityRef": payload.get("capabilityRef"),
                        "evidenceDigest": payload.get("evidenceDigest"),
                        "status": payload.get("status"),
                        "argumentsDigest": payload.get("argumentsDigest"),
                        "effect": payload.get("effect"),
                        "idempotent": payload.get("idempotent"),
                        "actionSignature": payload.get("actionSignature"),
                        "planStepId": payload.get("planStepId"),
                        "planDigest": payload.get("planDigest"),
                        "planRevision": payload.get("planRevision"),
                        "stepAttempt": payload.get("stepAttempt"),
                        "resultDigest": digest_json(
                            envelope["output"]
                            if payload.get("status") == "succeeded"
                            else envelope["error"]
                        ),
                        "storageKey": storage_key,
                        "stagingKey": staging_key,
                        "retentionClass": "checkpoint",
                    },
                )
            )
            created = True
        if created:
            target = (root / storage_key).resolve()
            if not target.is_relative_to(root):
                raise AgentHostError(
                    "EVIDENCE_CONFLICT", "ToolResult 最终路径越界"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != content_hash:
                    raise AgentHostError(
                        "EVIDENCE_CONFLICT",
                        f"ToolResult 最终文件 {storage_key} hash 冲突",
                    )
                staging.unlink(missing_ok=True)
            else:
                staging.replace(target)

    async def _persist_plan_snapshot_artifact(
        self,
        run_id: str,
        payload: dict[str, Any],
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(content) > 64 * 1024:
            raise AgentHostError(
                "PAYLOAD_TOO_LARGE", "Agent PlanSnapshot 超过 64 KiB"
            )
        content_hash = hashlib.sha256(content).hexdigest()
        revision = int(payload.get("revision", 1))
        storage_key = f"plans/revision-{revision}.json"
        token = claim_token or "unclaimed"
        staging_key = f".staging/{token}/{storage_key}"
        root = self._trajectory_dir(run_id)
        staging = (root / staging_key).resolve()
        if not staging.is_relative_to(root):
            raise AgentHostError("EVIDENCE_CONFLICT", "PlanSnapshot staging 路径越界")
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(content)
        artifact_id = str(uuid4())
        async with SessionLocal() as session, session.begin():
            owned = await session.scalar(
                select(ConversationRun.id).where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
            )
            if owned is None:
                staging.unlink(missing_ok=True)
                raise asyncio.CancelledError
            existing = await session.scalar(
                select(Artifact).where(
                    Artifact.conversation_run_id == run_id,
                    Artifact.kind == "agent_plan_snapshot",
                    Artifact.name == f"revision-{revision}.json",
                )
            )
            if existing is not None:
                staging.unlink(missing_ok=True)
                if existing.sha256 != content_hash:
                    raise AgentHostError(
                        "EVIDENCE_CONFLICT",
                        f"PlanSnapshot revision {revision} 内容冲突",
                    )
                return
            session.add(
                Artifact(
                    id=artifact_id,
                    run_id=None,
                    conversation_run_id=run_id,
                    kind="agent_plan_snapshot",
                    name=f"revision-{revision}.json",
                    uri=f"artifact://conversation-agents/{run_id}/{storage_key}",
                    sha256=content_hash,
                    size=len(content),
                    media_type="application/json",
                    metadata_json={
                        "schemaVersion": payload.get("schemaVersion"),
                        "planId": payload.get("planId"),
                        "revision": revision,
                        "planDigest": digest_json(payload),
                        "storageKey": storage_key,
                        "stagingKey": staging_key,
                        "retentionClass": "checkpoint",
                    },
                )
            )
        target = (root / storage_key).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != content_hash:
                raise AgentHostError(
                    "EVIDENCE_CONFLICT", "PlanSnapshot 最终文件 hash 冲突"
                )
            staging.unlink(missing_ok=True)
        else:
            staging.replace(target)

    async def _finalize_failure(
        self,
        run_id: str,
        error: AgentRunError,
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if run is None:
                return
            checkpoint = await self._create_task_checkpoint(
                session,
                run,
                {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "effectiveError": error.effective_error,
                    **(
                        {"reportedError": error.reported_error}
                        if error.reported_error
                        else {}
                    ),
                    **(
                        {"failedCallId": error.failed_call_id}
                        if error.failed_call_id
                        else {}
                    ),
                    **(
                        {"protocolViolation": error.protocol_violation}
                        if error.protocol_violation
                        else {}
                    ),
                },
                status="pending" if error.retryable else "terminal",
            )
            error_json = persisted_error(
                {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "effectiveError": error.effective_error,
                    **(
                        {"reportedError": error.reported_error}
                        if error.reported_error
                        else {}
                    ),
                    **(
                        {"failedCallId": error.failed_call_id}
                        if error.failed_call_id
                        else {}
                    ),
                    **(
                        {"protocolViolation": error.protocol_violation}
                        if error.protocol_violation
                        else {}
                    ),
                },
                technical_detail_ref=f"conversation-run:{run_id}",
            )
            error_json["taskCheckpointId"] = checkpoint.id
            claimed = await session.execute(
                update(ConversationRun)
                .where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
                .values(
                    status="failed",
                    finished_at=utcnow(),
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                    error_json=error_json,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                await session.delete(checkpoint)
                return
            runtime = agent_runtime_config()
            await record_model_usage(
                session,
                call_id=f"conversation-agent:{run_id}:{claim_generation}",
                stage="general_agent",
                turn_id=run.turn_id,
                conversation_run_id=run_id,
                provider=runtime.provider,
                model=runtime.model,
                protocol=runtime.protocol,
                runtime_type=runtime.type,
                adapter_version=runtime.adapter_version,
                adapter_digest=runtime.adapter_digest,
                runtime_used="agent_runtime",
                runtime_digest=runtime.digest,
                error_code=error.code,
                input_tokens=error.input_tokens,
                output_tokens=error.output_tokens,
                total_tokens=error.total_tokens,
                usage_complete=error.usage_complete,
                dispatched=error.request_dispatched,
                completed=False,
            )
            await _append_event(
                session,
                run_id,
                "agent.run.failed",
                {"conversationRunId": run_id, "error": error_json},
            )

    async def _finalize_cancelled(
        self,
        run_id: str,
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            error_json = persisted_error(
                {
                    "code": "CANCELLED",
                    "message": "Agent 运行已按请求取消",
                    "retryable": False,
                },
                technical_detail_ref=f"conversation-run:{run_id}",
            )
            claimed = await session.execute(
                update(ConversationRun)
                .where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
                .values(
                    status="cancelled",
                    finished_at=utcnow(),
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                    error_json=error_json,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                return
            await _append_event(
                session,
                run_id,
                "agent.run.cancelled",
                {"conversationRunId": run_id},
            )

    async def _finalize_success(
        self,
        run_id: str,
        config: AgentRunConfig,
        prompt: str,
        output: dict[str, Any],
        actions: list[dict[str, Any]],
        raw_events: list[dict[str, Any]],
        step_count: int,
        total_tokens: int,
        claim_generation: int,
        claim_token: str | None,
        *,
        usage_complete: bool = True,
        request_dispatched: bool = True,
        accepted_plan_digest: str | None = None,
        accepted_plan_revision: int | None = None,
        plan_step_states: dict[str, str] | None = None,
        plan_step_attempts: dict[str, list[dict[str, Any]]] | None = None,
        max_revisions: int = 1,
        used_revisions: int = 0,
        remaining_revisions: int = 1,
        provider: str | None = None,
        model: str | None = None,
        protocol: str | None = None,
        runtime_type: str | None = None,
        adapter_version: str | None = None,
        adapter_digest: str | None = None,
    ) -> None:
        # 先把原始轨迹写入 staging（不落最终目录），DB 只保存受控 artifact:// 引用与 hash。
        staged = self._stage_trajectory(
            run_id, raw_events, output, claim_token or "unclaimed"
        )
        trajectory = build_trajectory(config, prompt, output, actions, step_count, staged["uri"])
        trajectory["planLineage"] = {
            "acceptedPlanDigest": accepted_plan_digest,
            "acceptedPlanRevision": accepted_plan_revision,
            "stepStates": plan_step_states or {},
            "stepAttempts": plan_step_attempts or {},
            "replanBudget": {
                "maxRevisions": max_revisions,
                "usedRevisions": used_revisions,
                "remainingRevisions": remaining_revisions,
            },
        }
        summary = {
            "totalTokens": total_tokens,
            "steps": step_count,
            "toolCalls": [{"capability": item.get("capability")} for item in actions],
            "trajectoryArtifact": {
                "uri": staged["uri"],
                "sha256": staged["sha256"],
                "bytes": staged["bytes"],
            },
        }
        committed = False
        async with SessionLocal() as session, session.begin():
            claimed = await session.execute(
                update(ConversationRun)
                .where(
                    ConversationRun.id == run_id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == self.worker_id,
                    ConversationRun.claim_generation == claim_generation,
                    ConversationRun.claim_token == claim_token,
                )
                .values(
                    status="succeeded",
                    finished_at=utcnow(),
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                    output_json=output,
                    trajectory_json=trajectory,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                committed = False
            else:
                run_record = await session.get(ConversationRun, run_id)
                await record_model_usage(
                    session,
                    call_id=f"conversation-agent:{run_id}:{claim_generation}",
                    stage=(
                        "failure_continuation"
                        if run_record
                        and run_record.input_json.get(
                            "continuationOfConversationRunId"
                        )
                        else "general_agent"
                    ),
                    turn_id=run_record.turn_id if run_record else None,
                    conversation_run_id=run_id,
                    provider=provider or agent_runtime_config().provider,
                    model=model,
                    protocol=protocol,
                    runtime_type=runtime_type,
                    adapter_version=adapter_version,
                    adapter_digest=adapter_digest,
                    runtime_used="agent_runtime",
                    runtime_digest=agent_runtime_config().digest,
                    total_tokens=total_tokens if usage_complete else None,
                    usage_complete=usage_complete,
                    dispatched=request_dispatched,
                    completed=True,
                )
                session.add(
                    Artifact(
                        id=str(uuid4()),
                        run_id=None,
                        conversation_run_id=run_id,
                        kind="conversation_trajectory",
                        name="trajectory.json",
                        uri=staged["uri"],
                        sha256=staged["sha256"],
                        size=staged["bytes"],
                        media_type="application/json",
                        metadata_json={
                            "conversationRunId": run_id,
                            "sourceKind": "conversation_run",
                            "sourceId": run_id,
                            "storageKey": "trajectory.json",
                            "stagingKey": staged["stagingKey"],
                            "retentionClass": "trajectory",
                        },
                    )
                )
                await _append_event(
                    session,
                    run_id,
                    "agent.run.succeeded",
                    {"conversationRunId": run_id, "summary": summary},
                )
                committed = True
        # 事务提交后再从 staging 原子落位；提交失败则 staging 文件由 reconcile 清理。
        if committed:
            self._promote_trajectory(run_id, staged["stagingKey"])
        else:
            source = self._trajectory_dir(run_id) / staged["stagingKey"]
            source.unlink(missing_ok=True)

    def _owns(
        self,
        run: ConversationRun | None,
        claim_generation: int,
        claim_token: str | None,
    ) -> bool:
        return bool(
            run is not None
            and run.status == "running"
            and run.lease_owner == self.worker_id
            and run.claim_generation == claim_generation
            and run.claim_token == claim_token
        )

    @staticmethod
    async def _create_task_checkpoint(
        session: AsyncSession,
        run: ConversationRun,
        error: dict[str, Any],
        *,
        status: str,
    ) -> TaskCheckpoint:
        events = (
            await session.scalars(
                select(ConversationRunEvent)
                .where(ConversationRunEvent.conversation_run_id == run.id)
                .order_by(ConversationRunEvent.seq)
            )
        ).all()
        artifacts = (
            await session.scalars(
                select(Artifact).where(
                    Artifact.conversation_run_id == run.id,
                    Artifact.kind.in_(
                        ["tool_result", "agent_plan_snapshot"]
                    ),
                )
            )
        ).all()
        tool_artifacts = [
            item for item in artifacts if item.kind == "tool_result"
        ]
        expected_contract = run.input_json.get("expectedPlanContract")
        replan_budget = {
            "maxRevisions": int(
                (expected_contract or {}).get("maxRevisions", 1)
            ),
            "usedRevisions": int(
                (expected_contract or {}).get("usedRevisions", 0)
            ),
            "remainingRevisions": int(
                (expected_contract or {}).get("remainingRevisions", 1)
            ),
        }
        for item in events:
            persisted_budget = item.payload_json.get("replanBudget")
            if isinstance(persisted_budget, dict):
                replan_budget = {
                    "maxRevisions": int(
                        persisted_budget.get("maxRevisions", -1)
                    ),
                    "usedRevisions": int(
                        persisted_budget.get("usedRevisions", -1)
                    ),
                    "remainingRevisions": int(
                        persisted_budget.get("remainingRevisions", -1)
                    ),
                }
        budget_integrity_error = None
        if (
            min(replan_budget.values()) < 0
            or replan_budget["usedRevisions"]
            + replan_budget["remainingRevisions"]
            != replan_budget["maxRevisions"]
        ):
            budget_integrity_error = "replan revision budget 不一致"
        artifact_by_call = {
            str(item.metadata_json.get("callId")): item
            for item in tool_artifacts
        }
        requested = {
            item.payload_json.get("callId")
            for item in events
            if item.type == "tool.requested"
        }
        closed = {
            item.payload_json.get("callId")
            for item in events
            if item.type == "tool.result"
        }
        failed_results = [
            item
            for item in events
            if item.type == "tool.result"
            and item.payload_json.get("status") == "failed"
        ]
        failed_call_id = error.get("failedCallId") or (
            failed_results[-1].payload_json.get("callId")
            if failed_results
            else None
        )
        failed_artifact = artifact_by_call.get(str(failed_call_id))
        plan_artifacts = [
            item for item in artifacts if item.kind == "agent_plan_snapshot"
        ]
        plan_artifact = (
            max(
                plan_artifacts,
                key=lambda item: int(
                    item.metadata_json.get("revision", 0)
                ),
            )
            if plan_artifacts
            else None
        )
        plan_snapshot: dict[str, Any] | None = None
        plan_ref: dict[str, Any] | None = None
        plan_integrity_error: str | None = None
        if plan_artifact is not None:
            root = AgentRunWorker._trajectory_dir(run.id)
            try:
                path = safe_artifact_path(
                    root,
                    plan_artifact.metadata_json.get("storageKey"),
                    forbidden_root=root / ".staging",
                )
                if not artifact_record_matches(path, plan_artifact):
                    raise ValueError("PlanSnapshot Artifact 不存在或已损坏")
                plan_snapshot = json.loads(path.read_bytes())
                if digest_json(plan_snapshot) != plan_artifact.metadata_json.get(
                    "planDigest"
                ):
                    raise ValueError("PlanSnapshot digest 不一致")
                plan_ref = {
                    "id": plan_artifact.id,
                    "uri": plan_artifact.uri,
                    "sha256": plan_artifact.sha256,
                    "bytes": plan_artifact.size,
                    "digest": plan_artifact.metadata_json.get("planDigest"),
                    "schemaVersion": plan_artifact.metadata_json.get(
                        "schemaVersion"
                    ),
                    "revision": plan_artifact.metadata_json.get("revision"),
                }
            except (
                ArtifactPathError,
                ValueError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as exc:
                plan_integrity_error = str(exc)

        results_by_step: dict[str, list[ConversationRunEvent]] = {}
        for item in events:
            if item.type != "tool.result" or not item.payload_json.get(
                "planStepId"
            ):
                continue
            results_by_step.setdefault(
                str(item.payload_json["planStepId"]), []
            ).append(item)
        completed_steps: list[dict[str, Any]] = []
        failed_step: dict[str, Any] | None = None
        remaining_steps: list[dict[str, Any]] = []
        step_states: dict[str, str] = {}
        step_attempts: dict[str, list[dict[str, Any]]] = {}
        attempt_integrity_error: str | None = None
        if plan_snapshot is not None:
            for plan_step in plan_snapshot.get("steps", []):
                step_id = str(plan_step.get("id"))
                result_events = results_by_step.get(step_id, [])
                result_event = result_events[-1] if result_events else None
                result_status = (
                    result_event.payload_json.get("status")
                    if result_event is not None
                    else None
                )
                attempts = []
                for item in result_events:
                    call_id = str(item.payload_json.get("callId", ""))
                    artifact = artifact_by_call.get(call_id)
                    event_attempt = {
                        "planDigest": item.payload_json.get("planDigest"),
                        "stepId": item.payload_json.get("planStepId"),
                        "attempt": item.payload_json.get("stepAttempt"),
                        "callId": call_id,
                        "status": item.payload_json.get("status"),
                        "argumentsDigest": item.payload_json.get(
                            "argumentsDigest"
                        ),
                    }
                    artifact_attempt = (
                        {
                            "planDigest": artifact.metadata_json.get(
                                "planDigest"
                            ),
                            "stepId": artifact.metadata_json.get(
                                "planStepId"
                            ),
                            "attempt": artifact.metadata_json.get(
                                "stepAttempt"
                            ),
                            "callId": str(
                                artifact.metadata_json.get("callId", "")
                            ),
                            "status": artifact.metadata_json.get("status"),
                            "argumentsDigest": artifact.metadata_json.get(
                                "argumentsDigest"
                            ),
                        }
                        if artifact is not None
                        else None
                    )
                    if artifact is None or artifact_attempt != event_attempt:
                        attempt_integrity_error = (
                            f"step {step_id} call {call_id} 的 attempt "
                            "事件与 ToolResult Artifact metadata 冲突"
                        )
                    elif not isinstance(
                        event_attempt["argumentsDigest"], str
                    ) or not str(event_attempt["argumentsDigest"]).startswith(
                        "sha256:"
                    ):
                        attempt_integrity_error = (
                            f"step {step_id} call {call_id} 缺少 argumentsDigest"
                        )
                    else:
                        root = AgentRunWorker._trajectory_dir(run.id)
                        try:
                            artifact_path = safe_artifact_path(
                                root,
                                artifact.metadata_json.get("storageKey"),
                                forbidden_root=root / ".staging",
                            )
                            if not artifact_record_matches(
                                artifact_path, artifact
                            ):
                                raise ValueError(
                                    "ToolResult Artifact 不存在或已损坏"
                                )
                            envelope = json.loads(
                                artifact_path.read_bytes()
                            )
                            envelope_attempt = {
                                "planDigest": envelope.get("planDigest"),
                                "stepId": envelope.get("planStepId"),
                                "attempt": envelope.get("stepAttempt"),
                                "callId": str(envelope.get("callId", "")),
                                "status": envelope.get("status"),
                                "argumentsDigest": envelope.get(
                                    "argumentsDigest"
                                ),
                            }
                            if envelope_attempt != event_attempt:
                                raise ValueError(
                                    "ToolResult Artifact envelope 与事件冲突"
                                )
                        except (
                            ArtifactPathError,
                            ValueError,
                            json.JSONDecodeError,
                            UnicodeDecodeError,
                        ) as exc:
                            attempt_integrity_error = (
                                f"step {step_id} call {call_id}: {exc}"
                            )
                    attempts.append(
                        {
                            **event_attempt,
                            **(
                                {"error": item.payload_json.get("error")}
                                if item.payload_json.get("status") == "failed"
                                else {}
                            ),
                        }
                    )
                step_attempts[step_id] = attempts
                artifact = (
                    artifact_by_call.get(
                        str(result_event.payload_json.get("callId"))
                    )
                    if result_event is not None
                    else None
                )
                artifact_ref = (
                    {
                        "id": artifact.id,
                        "sha256": artifact.sha256,
                        "bytes": artifact.size,
                        "uri": artifact.uri,
                        "evidenceDigest": artifact.metadata_json.get(
                            "evidenceDigest"
                        ),
                    }
                    if artifact is not None
                    else None
                )
                state = (
                    "succeeded"
                    if result_status == "succeeded"
                    else "failed"
                    if result_status == "failed"
                    else "remaining"
                )
                entry = {
                    **plan_step,
                    "status": state,
                    **(
                        {
                            "callId": result_event.payload_json.get("callId"),
                            "artifact": artifact_ref,
                        }
                        if result_event is not None and artifact_ref is not None
                        else {}
                    ),
                }
                step_states[step_id] = state
                if state == "succeeded":
                    completed_steps.append(entry)
                elif state == "failed":
                    failed_step = {**entry, "error": error}
                    remaining_steps.append(entry)
                else:
                    remaining_steps.append(entry)
        elif plan_integrity_error is None:
            # Legacy runs are allowed to fail, but are never represented as a
            # checkpoint continuation from a frozen plan.
            remaining_steps = []

        pending_calls = sorted(
            str(call_id) for call_id in requested - closed if call_id
        )
        integrity_error = (
            plan_integrity_error
            or budget_integrity_error
            or attempt_integrity_error
        )
        remaining_plan = (
            {
                "mode": "integrity_failed",
                "reason": integrity_error,
                "steps": [],
            }
            if integrity_error
            else {
                "mode": "execute_remaining",
                "planDigest": plan_ref["digest"],
                "revision": plan_ref["revision"],
                "steps": remaining_steps,
            }
            if plan_ref is not None
            else {
                "mode": "replan_required",
                "reason": "源运行没有结构化 PlanSnapshot",
                "steps": [],
            }
        )
        action_ledger = [
            {
                "signature": item.metadata_json.get("actionSignature"),
                "capabilityRef": item.metadata_json.get("capabilityRef"),
                "effect": item.metadata_json.get("effect"),
                "idempotencyKey": item.metadata_json.get("idempotencyKey"),
                "resultArtifact": {
                    "id": item.id,
                    "sha256": item.sha256,
                    "resultDigest": item.metadata_json.get("resultDigest"),
                },
            }
            for item in tool_artifacts
            if item.metadata_json.get("actionSignature")
            and item.metadata_json.get("status") == "succeeded"
        ]
        artifact_refs = [
            {
                "id": item.id,
                "sha256": item.sha256,
                "bytes": item.size,
                "evidenceDigest": item.metadata_json.get("evidenceDigest"),
            }
            for item in tool_artifacts
        ]
        source_evidence = {
            "kind": "conversation_run",
            "id": run.id,
            "input": run.input_json,
            "config": run.config_json or {},
            "planSnapshot": plan_ref,
            "stepStates": step_states,
            "stepAttempts": step_attempts,
            "replanBudget": replan_budget,
            "artifactRefs": artifact_refs,
            "continuationGeneration": int(
                run.input_json.get("continuationGeneration", 0)
            ),
        }
        source_digest = digest_json(source_evidence)
        snapshot = {
            "source": {
                "kind": "conversation_run",
                "id": run.id,
                "digest": source_digest,
            },
            "rootSourceRunId": run.input_json.get(
                "rootSourceConversationRunId", run.id
            ),
            "sourceCheckpointId": run.input_json.get(
                "continuationOfCheckpointId"
            ),
            "sourceEvidence": source_evidence,
            "planSnapshot": plan_ref,
            "completedSteps": completed_steps,
            "failedAction": {
                "callId": failed_call_id,
                "error": error.get("effectiveError", error),
                **(
                    {
                        "artifact": {
                            "id": failed_artifact.id,
                            "sha256": failed_artifact.sha256,
                            "uri": failed_artifact.uri,
                            "evidenceDigest": failed_artifact.metadata_json.get(
                                "evidenceDigest"
                            ),
                        }
                    }
                    if failed_artifact is not None
                    else {}
                ),
            },
            "failedStep": failed_step
            or {"error": error.get("effectiveError", error)},
            "reportedError": error.get("reportedError"),
            "effectiveError": error.get("effectiveError", error),
            "pendingToolCalls": pending_calls,
            "remainingPlan": remaining_plan,
            "completedActionLedger": action_ledger,
            "stepStates": step_states,
            "stepAttempts": step_attempts,
            "stepStateDigest": digest_json(step_states),
            "replanBudget": replan_budget,
            "continuationGeneration": int(
                run.input_json.get("continuationGeneration", 0)
            ),
            "locks": list(
                (run.config_json or {}).get("allowedCapabilities", [])
            ),
            "committedSideEffects": [],
            "outcomeCertainty": (
                "unknown" if error.get("code") == "UNKNOWN_OUTCOME" else "known_failed"
            ),
            **(
                {
                    "pendingQuestion": (
                        "前一 Worker 的副作用结果未知。确认已核对外部状态并继续剩余步骤？"
                    )
                }
                if error.get("code") == "UNKNOWN_OUTCOME"
                else {}
            ),
            **(
                {
                    "pendingQuestion": (
                        "源运行没有结构化计划。确认允许 Host 在原能力锁和预算内"
                        "生成受限 PlanSnapshot revision 1？"
                    ),
                }
                if plan_ref is None and integrity_error is None
                else {}
            ),
            "userContext": {"message": run.input_json.get("message", "")},
        }
        checkpoint = TaskCheckpoint(
            id=str(uuid4()),
            source_kind="conversation_run",
            source_id=run.id,
            source_digest=source_digest,
            status=(
                "terminal"
                if integrity_error
                else "awaiting_user"
                if plan_ref is None and status == "pending"
                else status
            ),
            snapshot_json=snapshot,
            digest=digest_json(snapshot),
            claim_generation=0,
        )
        session.add(checkpoint)
        await session.flush()
        return checkpoint

    @staticmethod
    def _trajectory_dir(run_id: str) -> Any:
        return (get_settings().artifact_root / "conversation-agents" / run_id).resolve()

    @classmethod
    def _stage_trajectory(
        cls,
        run_id: str,
        raw_events: list[dict[str, Any]],
        output: dict[str, Any],
        claim_token: str,
    ) -> dict[str, Any]:
        from app.application.security import redact

        directory = cls._trajectory_dir(run_id)
        staging_key = f".staging/{claim_token}/trajectory.json"
        staging = directory / ".staging" / claim_token
        staging.mkdir(parents=True, exist_ok=True)
        # 默认 Local Demo 也不把 Secret 原值复制到磁盘：落盘的 runtime 事件与输出均脱敏。
        # 若确需原始轨迹调试价值，应经显式开关/分类/保留期限（本版本默认脱敏）。
        content = json.dumps(
            {"events": redact(raw_events), "output": redact(output)},
            ensure_ascii=False,
            indent=2,
        ).encode()
        (staging / "trajectory.json").write_bytes(content)
        return {
            "uri": f"artifact://conversation-agents/{run_id}/trajectory.json",
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "stagingKey": staging_key,
        }

    @classmethod
    def _promote_trajectory(
        cls, run_id: str, staging_key: str = ".staging/trajectory.json"
    ) -> None:
        directory = cls._trajectory_dir(run_id)
        source = directory / staging_key
        target = directory / "trajectory.json"
        if source.exists():
            source.replace(target)


def _desensitize_action(action: dict[str, Any]) -> dict[str, Any]:
    """对单个 action 脱敏：对 arguments/result 递归遮蔽敏感键，保留固化所需结构。

    保留 callId/kind/capability/argumentBindings/observations/outputSchema 等编译证据；
    只遮蔽 Secret 原值（password/token/apiKey 等），不整体丢弃可确定化的表数据。
    """

    from app.application.security import redact

    cleaned = dict(action)
    if isinstance(action.get("arguments"), dict):
        cleaned["arguments"] = redact(action["arguments"])
    if isinstance(action.get("result"), dict):
        cleaned["result"] = redact(action["result"])
    return cleaned


def build_trajectory(
    config: AgentRunConfig,
    prompt: str,
    output: dict[str, Any],
    actions: list[dict[str, Any]],
    step_count: int,
    artifact_uri: str | None = None,
) -> dict[str, Any]:
    """把一次成功的 Agent 运行归一化为可提炼的规范化轨迹（脱敏后入库）。

    保存脱敏后的 action 序列（callId/tool/结构化证据），Secret 原值不入库；供 Distillation
    消费并尝试参数化为确定节点，而不是简单地把原 prompt 再交给 Agent。
    """

    from app.application.security import redact

    cleaned_actions = [_desensitize_action(item) for item in actions]
    last_action_id = cleaned_actions[-1].get("callId") if cleaned_actions else None
    last_result = cleaned_actions[-1].get("result", {}) if cleaned_actions else {}
    last_observations = (
        cleaned_actions[-1].get("observations", {}) if cleaned_actions else {}
    )
    output_provenance: dict[str, Any] = {}
    for field, value in output.items():
        path = field
        if last_observations.get("operation") == "lookup":
            path = "value"
        elif isinstance(last_result, dict):
            if field in last_result:
                path = field
            else:
                matching = [
                    key for key, result_value in last_result.items() if result_value == value
                ]
                if len(matching) == 1:
                    path = matching[0]
        output_provenance[field] = {
            "source": "action_output",
            **({"actionId": last_action_id} if last_action_id else {}),
            "path": path,
        }
    return {
        "runType": "agent_run",
        "name": "conversation-agent-draft",
        "prompt": prompt,
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        "outputSchema": config.output_schema,
        "actions": cleaned_actions,
        "output": redact(output),
        "outputProvenance": output_provenance,
        "config": {
            "maxSteps": config.max_steps,
            "maxTotalTokens": config.max_total_tokens,
            "maxInputTokens": config.max_input_tokens,
            "maxOutputTokens": config.max_output_tokens,
            "maxBillableTokens": config.max_billable_tokens,
            "allowedCapabilities": list(config.allowed_capabilities),
        },
        "originalModelCalls": max(step_count, 1),
        "trajectoryArtifactUri": artifact_uri,
    }


def build_agent_run_message(
    conversation_run: ConversationRun,
    text: str,
    **context: Any,
) -> dict[str, Any]:
    """Build the persisted assistant contract shared by every AgentRun entry."""

    return {
        "type": "agent_run",
        "state": conversation_run.status,
        "conversationRunId": conversation_run.id,
        "runType": "agent_run",
        "text": text,
        **context,
    }


def summarize_run(conversation_run: ConversationRun) -> dict[str, Any]:
    """对话运行的 API 摘要视图（包含可恢复的长任务 handle 状态）。"""

    plan_lineage = (conversation_run.trajectory_json or {}).get(
        "planLineage", {}
    )
    browser_evidence = []
    for action in (conversation_run.trajectory_json or {}).get("actions", []):
        observations = action.get("observations") or {}
        result = action.get("result") or {}
        if action.get("kind") == "browser" and isinstance(result.get("content"), str):
            browser_evidence.append(
                {
                    "callId": action.get("callId"),
                    "resultSource": observations.get("resultSource"),
                    "contentSha256": hashlib.sha256(
                        result["content"].encode()
                    ).hexdigest(),
                }
            )
    return {
        "id": conversation_run.id,
        "conversationId": conversation_run.conversation_id,
        "status": conversation_run.status,
        "runType": conversation_run.run_type,
        "input": conversation_run.input_json,
        "output": conversation_run.output_json,
        "error": public_error(conversation_run.error_json),
        "attempt": conversation_run.attempt,
        "cancellationRequested": conversation_run.cancellation_requested_at is not None,
        "createdAt": rfc3339(conversation_run.created_at),
        "updatedAt": rfc3339(conversation_run.updated_at),
        "queuedAt": rfc3339(conversation_run.queued_at),
        "startedAt": rfc3339(conversation_run.started_at),
        "finishedAt": rfc3339(conversation_run.finished_at),
        "browserEvidence": browser_evidence,
        "stepAttempts": plan_lineage.get("stepAttempts", {}),
        "replanBudget": plan_lineage.get("replanBudget"),
    }
