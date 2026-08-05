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
    resolve_effective_capabilities,
)
from app.application.compiler import digest_json
from app.config import get_settings
from app.infrastructure.database import (
    Artifact,
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
    TaskCheckpoint,
    utcnow,
)
from app.runtime.adapters import TraeXSubprocessAdapter

FINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class AgentRunError(Exception):
    """AgentRunService 归一化过程中的确定性失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
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
    allowed_capabilities: tuple[str, ...] = ()
    max_duration_seconds: int = 120

    @staticmethod
    def default() -> AgentRunConfig:
        settings = get_settings()
        return AgentRunConfig(
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            },
            max_steps=settings.agent_max_steps,
            max_total_tokens=settings.agent_max_total_tokens,
            allowed_capabilities=("search.web",),
            max_duration_seconds=settings.agent_max_duration_seconds,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "outputSchema": self.output_schema,
            "maxSteps": self.max_steps,
            "maxTotalTokens": self.max_total_tokens,
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
    session.add(
        ConversationRunEvent(
            id=str(uuid4()),
            conversation_run_id=run_id,
            seq=seq,
            type=event_type,
            payload_json=payload,
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
        {"conversationRunId": conversation_run.id, "input": {"message": content}},
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
        adapter: TraeXSubprocessAdapter | None = None,
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

        async def event_sink(_runtime: dict[str, Any], audit: dict[str, Any]) -> None:
            # 只把脱敏的审计事件写入数据库；原始 payload 不落库。
            await self._append_committed(
                run_id,
                audit["type"],
                audit["payload"],
                claim_generation,
                claim_token,
            )

        async def cancellation() -> bool:
            return bool(signal)

        request = AgentHostRequest(
            run_id=run_id,
            prompt=prompt,
            output_schema=config.output_schema,
            working_directory=get_settings().artifact_root / "conversation-agents" / run_id,
            requested_capabilities=config.allowed_capabilities,
            max_steps=config.max_steps,
            max_total_tokens=config.max_total_tokens,
            max_duration_seconds=config.max_duration_seconds,
        )

        terminal: AgentRunError | None = None
        result = None
        try:
            result = await host.execute(
                request, event_sink=event_sink, cancellation=cancellation
            )
        except AgentHostError as exc:
            terminal = AgentRunError(exc.code, exc.message)
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

    async def _finalize_failure(
        self,
        run_id: str,
        error: AgentRunError,
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if not self._owns(run, claim_generation, claim_token):
                return
            checkpoint = await self._create_task_checkpoint(
                session,
                run,
                {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.code in {"TIMEOUT", "EXECUTION_FAILED"},
                },
                status=(
                    "pending"
                    if error.code in {"TIMEOUT", "EXECUTION_FAILED"}
                    else "terminal"
                ),
            )
            run.status = "failed"
            run.finished_at = utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            run.error_json = {
                "code": error.code,
                "message": error.message,
                "retryable": error.code in {"TIMEOUT", "EXECUTION_FAILED"},
                "taskCheckpointId": checkpoint.id,
            }
            await _append_event(
                session,
                run_id,
                "agent.run.failed",
                {"conversationRunId": run_id, "error": run.error_json},
            )

    async def _finalize_cancelled(
        self,
        run_id: str,
        claim_generation: int,
        claim_token: str | None,
    ) -> None:
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if not self._owns(run, claim_generation, claim_token):
                return
            run.status = "cancelled"
            run.finished_at = utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            run.error_json = {
                "code": "CANCELLED",
                "message": "Agent 运行已按请求取消",
                "retryable": False,
            }
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
    ) -> None:
        # 先把原始轨迹写入 staging（不落最终目录），DB 只保存受控 artifact:// 引用与 hash。
        async with SessionLocal() as session:
            run = await session.get(ConversationRun, run_id)
            if not self._owns(run, claim_generation, claim_token):
                return
        staged = self._stage_trajectory(
            run_id, raw_events, output, claim_token or "unclaimed"
        )
        trajectory = build_trajectory(config, prompt, output, actions, step_count, staged["uri"])
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
            run = await session.get(ConversationRun, run_id)
            if not self._owns(run, claim_generation, claim_token):
                return
            run.status = "succeeded"
            run.finished_at = utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            run.output_json = output
            run.trajectory_json = trajectory
            session.add(
                Artifact(
                    id=str(uuid4()),
                    run_id=None,
                    conversation_run_id=run.id,
                    kind="conversation_trajectory",
                    name="trajectory.json",
                    uri=staged["uri"],
                    sha256=staged["sha256"],
                    size=staged["bytes"],
                    media_type="application/json",
                    metadata_json={
                        "conversationRunId": run.id,
                        "sourceKind": "conversation_run",
                        "sourceId": run.id,
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
        source = {
            "kind": "conversation_run",
            "id": run.id,
            "input": run.input_json,
            "config": run.config_json or {},
            "trajectory": run.trajectory_json or {},
        }
        source_digest = digest_json(source)
        snapshot = {
            "source": {
                "kind": "conversation_run",
                "id": run.id,
                "digest": source_digest,
            },
            "completedSteps": [
                {
                    "seq": item.seq,
                    "type": item.type,
                    "payload": item.payload_json,
                }
                for item in events
                if item.type in {"tool.result", "agent.step.completed"}
            ],
            "failedStep": {"error": error},
            "pendingToolCalls": sorted(
                str(call_id) for call_id in requested - closed if call_id
            ),
            "remainingPlan": {"resumeSameTask": True},
            "locks": list(
                (run.config_json or {}).get("allowedCapabilities", [])
            ),
            "committedSideEffects": [],
            "outcomeCertainty": (
                "unknown" if error.get("code") == "UNKNOWN_OUTCOME" else "known_failed"
            ),
            "userContext": {"message": run.input_json.get("message", "")},
        }
        checkpoint = TaskCheckpoint(
            id=str(uuid4()),
            source_kind="conversation_run",
            source_id=run.id,
            source_digest=source_digest,
            status=status,
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
            "allowedCapabilities": list(config.allowed_capabilities),
        },
        "originalModelCalls": max(step_count, 1),
        "trajectoryArtifactUri": artifact_uri,
    }


def summarize_run(conversation_run: ConversationRun) -> dict[str, Any]:
    """对话运行的 API 摘要视图（包含可恢复的长任务 handle 状态）。"""

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
        "error": conversation_run.error_json,
        "attempt": conversation_run.attempt,
        "cancellationRequested": conversation_run.cancellation_requested_at is not None,
        "browserEvidence": browser_evidence,
    }
