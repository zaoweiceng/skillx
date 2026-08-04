from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.application.agent_events import AgentEventError, AgentEventValidator
from app.config import get_settings
from app.runtime.adapters import AdapterError, AgentRunRequest, TraeXSubprocessAdapter

# 005-A：唯一的 Agent 信任边界。
#
# ConversationRun 与 Workflow ``AgentExecutor`` 都通过 ``AgentHost.execute`` 运行受限
# TraeX，从而在两个入口得到完全一致的安全语义：requested ∩ host_granted、事件判别
# 联合、callId 配对、step/Token/time/payload 上限、输出 Schema 与取消。差异只在事件
# 持久化 sink（ConversationRun 写 conversation_run_events；NodeRun 走 NodeResult.events）。


class AgentHostError(Exception):
    """AgentHost 执行过程中的确定性失败，携带稳定 error code。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class AgentHostRequest:
    """一次受限 Agent 运行的完整请求。"""

    run_id: str
    prompt: str
    output_schema: dict[str, Any]
    working_directory: Path
    requested_capabilities: tuple[str, ...] = ()
    max_steps: int = 6
    max_total_tokens: int = 30000
    max_duration_seconds: int = 120


@dataclass
class AgentHostResult:
    """AgentHost 成功执行后的结构化结果。"""

    output: dict[str, Any]
    actions: list[dict[str, Any]] = field(default_factory=list)
    runtime_events: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    total_tokens: int = 0


# event_sink 接收 (runtime_event, audit_event)，由调用方决定如何持久化。
EventSink = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]
# cancellation 返回 True 表示应立即停止（取消/租约丢失）。
CancellationCheck = Callable[[], Awaitable[bool]]


def resolve_effective_capabilities(requested: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """实际能力 = 请求集合 ∩ Host 授予集合。

    返回 ``(effective, denied)``。请求方无法通过 body/config 扩大 Tool 范围。本版本按
    “任务所需集合”处理：存在 denied 时由调用方在执行前 fail closed（PERMISSION_DENIED），
    不产生误导性成功。
    """

    host_granted = set(get_settings().agent_host_capabilities)
    effective = [item for item in requested if item in host_granted]
    denied = [item for item in requested if item not in host_granted]
    return effective, denied


class AgentHost:
    """受限 Agent 执行服务。两个入口共享同一实例语义。"""

    def __init__(self, adapter: TraeXSubprocessAdapter | None = None) -> None:
        self._adapter = adapter

    def _resolve_adapter(self) -> TraeXSubprocessAdapter:
        if self._adapter is not None:
            return self._adapter
        settings = get_settings()
        if not settings.traex_command:
            raise AgentHostError("MODEL_NOT_CONFIGURED", "TraeX Runtime 尚未配置")
        return TraeXSubprocessAdapter(
            settings.traex_command,
            set(settings.traex_allowed_executables),
        )

    async def execute(
        self,
        request: AgentHostRequest,
        *,
        event_sink: EventSink | None = None,
        cancellation: CancellationCheck | None = None,
    ) -> AgentHostResult:
        """执行受限 Agent。任何契约/权限/预算违规都抛 :class:`AgentHostError`。

        ``asyncio.CancelledError`` 透传给调用方（用于超时/取消语义）。
        """

        # 请求能力必须是 Host 授予的子集：存在越权能力则执行前 fail closed。
        effective, denied = resolve_effective_capabilities(request.requested_capabilities)
        if denied:
            raise AgentHostError(
                "PERMISSION_DENIED",
                f"请求能力 {denied} 未被 Host 授予，拒绝执行",
            )

        adapter = self._resolve_adapter()
        settings = get_settings()
        validator = AgentEventValidator(
            set(effective),
            max_payload_bytes=settings.agent_max_event_payload_bytes,
        )
        run_request = AgentRunRequest(
            prompt=request.prompt,
            working_directory=request.working_directory,
            allowed_tools=effective,
            max_steps=request.max_steps,
            max_tokens=request.max_total_tokens,
        )

        result = AgentHostResult(output={})
        open_calls: dict[str, dict[str, Any]] = {}
        output: dict[str, Any] | None = None

        try:
            async with asyncio.timeout(request.max_duration_seconds):
                async for raw in adapter.events(request.run_id, run_request):
                    if cancellation is not None and await cancellation():
                        await self._cancel(adapter, request.run_id)
                        raise asyncio.CancelledError
                    try:
                        runtime_event, audit_event = validator.validate_pair(raw)
                    except PermissionError as exc:
                        await self._cancel(adapter, request.run_id)
                        raise AgentHostError(
                            "PERMISSION_DENIED",
                            f"Agent 调用了未授权能力 {exc}，不在生效能力集合内",
                        ) from exc
                    except AgentEventError as exc:
                        await self._cancel(adapter, request.run_id)
                        raise AgentHostError("AGENT_EVENT_INVALID", str(exc)) from exc

                    result.runtime_events.append(runtime_event)
                    result.audit_events.append(audit_event)
                    if event_sink is not None:
                        await event_sink(runtime_event, audit_event)

                    event_type = runtime_event["type"]
                    payload = runtime_event["payload"]
                    if event_type in ("tool.call.started", "browser.action.started"):
                        open_calls[payload["callId"]] = payload
                    elif event_type in ("tool.call.completed", "browser.action.completed"):
                        started = open_calls.pop(payload["callId"], {})
                        result.actions.append(
                            self._normalize_action(event_type, started, payload)
                        )
                    elif event_type == "agent.step.completed":
                        result.step_count += 1
                        result.total_tokens += payload.get("inputTokens", 0) + payload.get(
                            "outputTokens", 0
                        )
                        if result.step_count > request.max_steps:
                            await self._cancel(adapter, request.run_id)
                            raise AgentHostError(
                                "STEP_BUDGET_EXCEEDED",
                                f"Agent 步数超过 Host 上限 {request.max_steps}",
                            )
                        if result.total_tokens > request.max_total_tokens:
                            await self._cancel(adapter, request.run_id)
                            raise AgentHostError(
                                "TOKEN_BUDGET_EXCEEDED",
                                f"Agent Token 超过 Host 上限 {request.max_total_tokens}",
                            )
                    elif event_type == "agent.completed":
                        output = payload.get("output")
                        if payload.get("totalTokens") is not None:
                            result.total_tokens = payload["totalTokens"]
        except TimeoutError as exc:
            await self._cancel(adapter, request.run_id)
            raise AgentHostError("TIMEOUT", "Agent 运行超时，已取消") from exc
        except AdapterError as exc:
            raise AgentHostError("EXECUTION_FAILED", str(exc)) from exc

        if output is None:
            raise AgentHostError("EXECUTION_FAILED", "Agent 未返回结构化输出")
        errors = list(Draft202012Validator(request.output_schema).iter_errors(output))
        if errors:
            raise AgentHostError(
                "CONTRACT_VIOLATION",
                f"Agent 输出不符合 outputSchema: {errors[0].message}",
            )
        result.output = output
        return result

    @staticmethod
    def _normalize_action(
        event_type: str,
        started: dict[str, Any],
        completed: dict[str, Any],
    ) -> dict[str, Any]:
        """把一对 started/completed 事件归一化为动作记录（供 IR 构建消费）。

        透传事件可选携带的固化证据：``capabilityRef``（已发布能力锁）、``outputSchema``、
        ``replayability`` 与 ``observations``（如 transform lookup 的 operation/table）。
        这些是 Compiler 做确定化裁决的证据来源，而非依赖“结果恰好是 dict”。
        """

        if event_type == "browser.action.completed":
            kind = "browser"
            capability = completed.get("capability", "")
        else:
            kind = "tool"
            capability = completed.get("tool", "")
        action: dict[str, Any] = {
            "callId": completed["callId"],
            "kind": completed.get("actionKind") or kind,
            "capability": capability,
            "action": completed.get("action") or started.get("action"),
            "arguments": started.get("arguments", {}),
            "result": completed.get("result", {}),
        }
        # 透传固化证据（如果 Runtime 提供）。
        for key in ("capabilityRef", "outputSchema", "replayability", "effect", "idempotent"):
            if key in completed:
                action[key] = completed[key]
            elif key in started:
                action[key] = started[key]
        observations = completed.get("observations") or started.get("observations")
        if isinstance(observations, dict):
            action["observations"] = observations
        return action

    @staticmethod
    async def _cancel(adapter: TraeXSubprocessAdapter, run_id: str) -> None:
        try:
            await adapter.cancel(run_id)
        except Exception:  # noqa: BLE001 - 取消失败不应掩盖原始终态。
            pass
