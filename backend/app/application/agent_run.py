from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_events import AgentEventError, AgentEventValidator
from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
    utcnow,
)
from app.runtime.adapters import AdapterError, AgentRunRequest, TraeXSubprocessAdapter

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


def resolve_effective_capabilities(requested: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """实际能力 = 请求集合 ∩ Host 授予集合。

    返回 ``(effective, denied)``。请求方无法通过 body 扩大 Tool 范围；越权的能力被记录
    为 denied 并在运行中作为 ``PERMISSION_DENIED`` fail closed。
    """

    host_granted = set(get_settings().agent_host_capabilities)
    effective = [item for item in requested if item in host_granted]
    denied = [item for item in requested if item not in host_granted]
    return effective, denied


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

    def _resolve_adapter(self) -> TraeXSubprocessAdapter:
        if self._adapter is not None:
            return self._adapter
        settings = get_settings()
        if not settings.traex_command:
            raise AgentRunError("MODEL_NOT_CONFIGURED", "TraeX Runtime 尚未配置")
        return TraeXSubprocessAdapter(
            settings.traex_command,
            set(settings.traex_allowed_executables),
        )

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
            result = await session.execute(
                update(ConversationRun)
                .where(
                    ConversationRun.id == run.id,
                    ConversationRun.status == "running",
                    ConversationRun.lease_owner == run.lease_owner,
                )
                .values(
                    status="failed",
                    lease_owner=None,
                    lease_expires_at=None,
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
            .where(ConversationRun.id == candidate.id, ConversationRun.status == "queued")
            .values(
                status="running",
                lease_owner=self.worker_id,
                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                heartbeat_at=now,
                started_at=now,
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

        try:
            adapter = self._resolve_adapter()
        except AgentRunError as exc:
            await self._finalize_failure(run_id, exc)
            return

        effective, denied = resolve_effective_capabilities(config.allowed_capabilities)
        if denied:
            await self._append_committed(
                run_id,
                "agent.capabilities.narrowed",
                {
                    "requested": list(config.allowed_capabilities),
                    "denied": denied,
                    "granted": effective,
                },
            )

        request = AgentRunRequest(
            prompt=prompt,
            working_directory=(
                get_settings().artifact_root / "conversation-agents" / run_id
            ),
            allowed_tools=effective,
            max_steps=config.max_steps,
            max_tokens=config.max_total_tokens,
        )
        validator = AgentEventValidator(
            set(effective),
            max_payload_bytes=get_settings().agent_max_event_payload_bytes,
        )

        actions: list[dict[str, Any]] = []
        open_calls: dict[str, dict[str, Any]] = {}
        raw_events: list[dict[str, Any]] = []
        output: dict[str, Any] | None = None
        total_tokens = 0
        step_count = 0

        # 由后台 watcher 传播取消/租约丢失，主循环观察 signal 决定终态。
        signal: dict[str, Any] = {}
        stop_watch = asyncio.Event()
        watcher = asyncio.create_task(self._watch(run_id, adapter, signal, stop_watch))

        terminal: AgentRunError | None = None
        try:
            async with asyncio.timeout(config.max_duration_seconds):
                async for raw in adapter.events(run_id, request):
                    if signal:
                        break
                    try:
                        event = validator.validate(raw)
                    except PermissionError as exc:
                        terminal = AgentRunError(
                            "PERMISSION_DENIED",
                            f"Agent 调用了未授权工具 {exc}，不在生效能力集合内",
                        )
                        await self._cancel_adapter(adapter, run_id)
                        break
                    except AgentEventError as exc:
                        terminal = AgentRunError("AGENT_EVENT_INVALID", str(exc))
                        await self._cancel_adapter(adapter, run_id)
                        break
                    raw_events.append(event)
                    await self._append_committed(run_id, event["type"], event["payload"])
                    payload = event["payload"]
                    if event["type"] == "tool.call.started":
                        open_calls[payload["callId"]] = payload
                    elif event["type"] == "tool.call.completed":
                        started = open_calls.pop(payload["callId"], {})
                        actions.append(
                            {
                                "callId": payload["callId"],
                                "tool": payload["tool"],
                                "arguments": started.get("arguments", {}),
                                "result": payload.get("result", {}),
                            }
                        )
                    elif event["type"] == "agent.step.completed":
                        step_count += 1
                        total_tokens += payload.get("inputTokens", 0) + payload.get(
                            "outputTokens", 0
                        )
                        if step_count > config.max_steps:
                            terminal = AgentRunError(
                                "STEP_BUDGET_EXCEEDED",
                                f"Agent 步数超过 Host 上限 {config.max_steps}",
                            )
                            await self._cancel_adapter(adapter, run_id)
                            break
                        if total_tokens > config.max_total_tokens:
                            terminal = AgentRunError(
                                "TOKEN_BUDGET_EXCEEDED",
                                f"Agent Token 超过 Host 上限 {config.max_total_tokens}",
                            )
                            await self._cancel_adapter(adapter, run_id)
                            break
                    elif event["type"] == "agent.completed":
                        output = payload.get("output")
                        if payload.get("totalTokens") is not None:
                            total_tokens = payload["totalTokens"]
        except TimeoutError:
            terminal = AgentRunError("TIMEOUT", "Agent 运行超时，已取消")
            await self._cancel_adapter(adapter, run_id)
        except AdapterError as exc:
            if not signal and terminal is None:
                terminal = AgentRunError("EXECUTION_FAILED", str(exc))
        finally:
            stop_watch.set()
            await watcher

        # 取消/丢失租约优先于其它终态。
        if signal.get("kind") == "lost":
            return  # 已被恢复或另一个 Worker 接管，不再写入终态。
        if signal.get("kind") == "cancelled":
            await self._finalize_cancelled(run_id)
            return
        if terminal is not None:
            await self._finalize_failure(run_id, terminal)
            return
        if output is None:
            await self._finalize_failure(
                run_id, AgentRunError("EXECUTION_FAILED", "Agent 未返回结构化输出")
            )
            return
        errors = list(Draft202012Validator(config.output_schema).iter_errors(output))
        if errors:
            await self._finalize_failure(
                run_id,
                AgentRunError(
                    "CONTRACT_VIOLATION",
                    f"Agent 输出不符合 outputSchema: {errors[0].message}",
                ),
            )
            return
        await self._finalize_success(
            run_id,
            config,
            prompt,
            output,
            actions,
            raw_events,
            step_count,
            total_tokens,
        )

    async def _watch(
        self,
        run_id: str,
        adapter: TraeXSubprocessAdapter,
        signal: dict[str, Any],
        stop: asyncio.Event,
    ) -> None:
        """周期性刷新租约并观察取消请求；必要时终止子进程。"""

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
                    signal["kind"] = "cancelled"
                    await self._cancel_adapter(adapter, run_id)
                    break
                result = await session.execute(
                    update(ConversationRun)
                    .where(
                        ConversationRun.id == run_id,
                        ConversationRun.status == "running",
                        ConversationRun.lease_owner == self.worker_id,
                    )
                    .values(
                        heartbeat_at=utcnow(),
                        lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds),
                    )
                )
                if result.rowcount != 1:
                    signal["kind"] = "lost"
                    await self._cancel_adapter(adapter, run_id)
                    break

    async def _append_committed(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        async with SessionLocal() as session, session.begin():
            await _append_event(session, run_id, event_type, payload)

    @staticmethod
    async def _cancel_adapter(adapter: TraeXSubprocessAdapter, run_id: str) -> None:
        try:
            await adapter.cancel(run_id)
        except Exception:  # noqa: BLE001 - 取消失败不应掩盖原始终态。
            pass

    async def _finalize_failure(self, run_id: str, error: AgentRunError) -> None:
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if run is None or run.status in FINAL_STATES:
                return
            run.status = "failed"
            run.finished_at = utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            run.error_json = {
                "code": error.code,
                "message": error.message,
                "retryable": error.code in {"TIMEOUT", "EXECUTION_FAILED"},
            }
            await _append_event(
                session,
                run_id,
                "agent.run.failed",
                {"conversationRunId": run_id, "error": run.error_json},
            )

    async def _finalize_cancelled(self, run_id: str) -> None:
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if run is None or run.status in FINAL_STATES:
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
    ) -> None:
        # 原始轨迹写入 Artifact 文件，数据库只保存脱敏摘要与引用。
        artifact_uri = self._write_trajectory_artifact(run_id, raw_events, output)
        trajectory = build_trajectory(config, prompt, output, actions, step_count, artifact_uri)
        summary = {
            "totalTokens": total_tokens,
            "steps": step_count,
            "toolCalls": [{"tool": item["tool"]} for item in actions],
            "trajectoryArtifactUri": artifact_uri,
        }
        async with SessionLocal() as session, session.begin():
            run = await session.get(ConversationRun, run_id)
            if run is None or run.status in FINAL_STATES:
                return
            run.status = "succeeded"
            run.finished_at = utcnow()
            run.lease_owner = None
            run.lease_expires_at = None
            run.output_json = output
            run.trajectory_json = trajectory
            await _append_event(
                session,
                run_id,
                "agent.run.succeeded",
                {"conversationRunId": run_id, "summary": summary},
            )

    @staticmethod
    def _write_trajectory_artifact(
        run_id: str, raw_events: list[dict[str, Any]], output: dict[str, Any]
    ) -> str:
        directory = (get_settings().artifact_root / "conversation-agents" / run_id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "trajectory.json"
        path.write_text(
            json.dumps({"events": raw_events, "output": output}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return f"file://{path}"


def build_trajectory(
    config: AgentRunConfig,
    prompt: str,
    output: dict[str, Any],
    actions: list[dict[str, Any]],
    step_count: int,
    artifact_uri: str | None = None,
) -> dict[str, Any]:
    """把一次成功的 Agent 运行归一化为可提炼的规范化轨迹。

    保存成功 action 序列（callId/tool/参数/结果），供 Distillation 消费并尝试参数化为
    确定节点，而不是简单地把原 prompt 再交给 Agent。
    """

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
        "actions": actions,
        "output": output,
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
    }
