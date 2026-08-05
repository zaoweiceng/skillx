from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from app.application.agent_events import AgentEventError, AgentEventValidator
from app.application.agent_plans import (
    AgentPlanError,
    AgentPlanSnapshot,
    apply_replan_patch,
    validate_agent_plan,
)
from app.application.error_policy import error_policy
from app.application.tool_broker import (
    ToolAdapterRegistry,
    ToolBrokerError,
    ToolExecutionContext,
    evidence_digest,
)
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

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        failed_call_id: str | None = None,
        protocol_violation: str | None = None,
        reported_error: dict[str, Any] | None = None,
    ) -> None:
        policy = error_policy(code)
        self.code = policy.code
        self.message = message
        # retryable is deliberately advisory. The Host policy owns the effective
        # state transition even when an Agent or Tool payload reports otherwise.
        self.retryable = policy.retryable
        self.failed_call_id = failed_call_id
        self.protocol_violation = protocol_violation
        self.reported_error = reported_error
        self.effective_error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
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
    completed_actions: tuple[dict[str, Any], ...] = ()


@dataclass
class AgentHostResult:
    """AgentHost 成功执行后的结构化结果。"""

    output: dict[str, Any]
    actions: list[dict[str, Any]] = field(default_factory=list)
    runtime_events: list[dict[str, Any]] = field(default_factory=list)
    audit_events: list[dict[str, Any]] = field(default_factory=list)
    step_count: int = 0
    total_tokens: int = 0
    plan_snapshot: dict[str, Any] | None = None


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

    def __init__(
        self,
        adapter: TraeXSubprocessAdapter | None = None,
        tool_registry: ToolAdapterRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._tool_registry = tool_registry or ToolAdapterRegistry()

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
        broker_results: set[str] = set()
        broker_payloads: dict[str, dict[str, Any]] = {}
        failed_tool_result: dict[str, Any] | None = None
        output: dict[str, Any] | None = None
        plan: AgentPlanSnapshot | None = None
        completed_plan_steps: set[str] = set()

        try:
            async with asyncio.timeout(request.max_duration_seconds):
                event_iter = adapter.events(request.run_id, run_request).__aiter__()
                while True:
                    # 事件流与取消检查并发竞争：即便子进程挂住不再产出事件，取消一到
                    # 也能立即终止 subprocess，而不必等待下一条事件（P2-2 / E-01）。
                    next_task = asyncio.ensure_future(event_iter.__anext__())
                    cancel_task: asyncio.Task[bool] | None = None
                    if cancellation is not None:
                        cancel_task = asyncio.ensure_future(self._await_cancel(cancellation))
                    wait_set = {next_task, *( [cancel_task] if cancel_task else [] )}
                    done, _pending = await asyncio.wait(
                        wait_set, return_when=asyncio.FIRST_COMPLETED
                    )
                    if cancel_task is not None and cancel_task in done and cancel_task.result():
                        next_task.cancel()
                        with suppress(asyncio.CancelledError, StopAsyncIteration):
                            await next_task
                        await self._cancel(adapter, request.run_id)
                        raise asyncio.CancelledError
                    if cancel_task is not None and cancel_task not in done:
                        cancel_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await cancel_task
                    try:
                        raw = await next_task
                    except StopAsyncIteration:
                        break
                    if raw.get("type") == "tool.result":
                        await self._cancel(adapter, request.run_id)
                        raise AgentHostError(
                            "AGENT_EVENT_INVALID",
                            "tool.result 只能由 Host 生成，Agent 不得自报",
                        )
                    if raw.get("type") == "tool.requested":
                        try:
                            runtime_event, audit_event = validator.validate_pair(raw)
                        except PermissionError as exc:
                            await self._cancel(adapter, request.run_id)
                            raise AgentHostError(
                                "PERMISSION_DENIED",
                                f"Agent 调用了未授权能力 {exc}",
                            ) from exc
                        except AgentEventError as exc:
                            await self._cancel(adapter, request.run_id)
                            raise AgentHostError("AGENT_EVENT_INVALID", str(exc)) from exc
                        result.runtime_events.append(runtime_event)
                        result.audit_events.append(audit_event)
                        if event_sink is not None:
                            await event_sink(runtime_event, audit_event)
                        request_payload = runtime_event["payload"]
                        if plan is not None:
                            self._validate_planned_tool_request(
                                plan,
                                request_payload,
                                completed_plan_steps,
                            )
                        open_calls[request_payload["callId"]] = request_payload
                        tool_result = await self._execute_tool_request(
                            request, request_payload
                        )
                        await adapter.send_tool_result(request.run_id, tool_result)
                        runtime_result, audit_result = validator.validate_pair(tool_result)
                        result.runtime_events.append(runtime_result)
                        result.audit_events.append(audit_result)
                        if event_sink is not None:
                            await event_sink(runtime_result, audit_result)
                        started = open_calls.pop(request_payload["callId"])
                        result.actions.append(
                            self._normalize_action(
                                "tool.result", started, runtime_result["payload"]
                            )
                        )
                        broker_results.add(request_payload["callId"])
                        broker_payloads[request_payload["callId"]] = runtime_result[
                            "payload"
                        ]
                        if runtime_result["payload"].get("status") == "failed":
                            failed_tool_result = runtime_result["payload"]
                        elif request_payload.get("planStepId"):
                            completed_plan_steps.add(request_payload["planStepId"])
                        continue
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
                        if (
                            failed_tool_result is not None
                            and raw.get("type") == "agent.completed"
                        ):
                            raise self._failed_tool_error(
                                failed_tool_result,
                                protocol_violation=str(exc),
                            ) from exc
                        raise AgentHostError("AGENT_EVENT_INVALID", str(exc)) from exc

                    event_type = runtime_event["type"]
                    payload = runtime_event["payload"]
                    if event_type == "agent.plan" and isinstance(
                        payload.get("steps"), list
                    ):
                        if plan is not None:
                            raise AgentHostError(
                                "AGENT_EVENT_INVALID",
                                "同一 AgentRun 不得直接替换计划；必须提交 ReplanPatch",
                            )
                        try:
                            plan = validate_agent_plan(
                                payload,
                                allowed_capabilities=set(effective),
                                max_steps=request.max_steps,
                            )
                        except AgentPlanError as exc:
                            await self._cancel(adapter, request.run_id)
                            raise AgentHostError(
                                "AGENT_EVENT_INVALID", str(exc)
                            ) from exc
                        for planned_step in plan.steps:
                            try:
                                descriptor = self._tool_registry.describe(
                                    planned_step.capabilityRef
                                )
                            except ToolBrokerError:
                                continue
                            if descriptor["effect"] != planned_step.effect:
                                raise AgentHostError(
                                    "AGENT_EVENT_INVALID",
                                    f"计划步骤 {planned_step.id} 的 effect 与 Host 能力契约不一致",
                                )
                        result.plan_snapshot = plan.canonical()
                    elif event_type == "agent.replan":
                        if plan is None:
                            raise AgentHostError(
                                "AGENT_EVENT_INVALID",
                                "agent.replan 必须引用已验证的 PlanSnapshot",
                            )
                        try:
                            plan = apply_replan_patch(
                                plan,
                                payload,
                                completed_step_ids=completed_plan_steps,
                                allowed_capabilities=set(effective),
                                max_steps=request.max_steps,
                            )
                        except AgentPlanError as exc:
                            raise AgentHostError(
                                "AGENT_EVENT_INVALID", str(exc)
                            ) from exc
                        # Persist the immutable replacement revision rather than
                        # the patch envelope as the executable source of truth.
                        runtime_event = {
                            "type": "agent.plan",
                            "payload": plan.canonical(),
                        }
                        audit_event = {
                            "type": "agent.replan",
                            "payload": {
                                **audit_event["payload"],
                                "newPlanDigest": plan.digest,
                                "newRevision": plan.revision,
                            },
                        }
                        result.plan_snapshot = plan.canonical()
                    result.runtime_events.append(runtime_event)
                    result.audit_events.append(audit_event)
                    if event_sink is not None:
                        await event_sink(runtime_event, audit_event)

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
                        if broker_results:
                            try:
                                self._validate_output_provenance(
                                    output,
                                    payload.get("outputProvenance"),
                                    broker_payloads,
                                )
                            except AgentHostError as exc:
                                await self._cancel(adapter, request.run_id)
                                if failed_tool_result is not None:
                                    raise self._failed_tool_error(
                                        failed_tool_result,
                                        protocol_violation=exc.message,
                                    ) from exc
                                raise
                        final_tokens = payload.get("totalTokens")
                        if final_tokens is not None:
                            # 终态 Token 一致性校验：必须非负、不得小于可信步骤累计值，
                            # 并再次执行 Host 上限（只在终态报大数或报更小数都不能绕过）。
                            if not isinstance(final_tokens, int) or final_tokens < 0:
                                await self._cancel(adapter, request.run_id)
                                raise AgentHostError(
                                    "AGENT_EVENT_INVALID",
                                    f"agent.completed.totalTokens 非法: {final_tokens}",
                                )
                            if final_tokens < result.total_tokens:
                                await self._cancel(adapter, request.run_id)
                                raise AgentHostError(
                                    "AGENT_EVENT_INVALID",
                                    "agent.completed.totalTokens 小于步骤累计值，审计不一致",
                                )
                            if final_tokens > request.max_total_tokens:
                                await self._cancel(adapter, request.run_id)
                                raise AgentHostError(
                                    "TOKEN_BUDGET_EXCEEDED",
                                    f"终态 Token {final_tokens} 超过 Host 上限 "
                                    f"{request.max_total_tokens}",
                                )
                            result.total_tokens = final_tokens
                    elif event_type == "agent.failed":
                        await self._cancel(adapter, request.run_id)
                        reported_error = {
                            "code": payload["code"],
                            "message": payload["message"],
                            "retryable": payload["retryable"],
                            "callId": payload.get("callId"),
                        }
                        if failed_tool_result is not None:
                            call_id = payload.get("callId")
                            if call_id != failed_tool_result.get("callId"):
                                raise self._failed_tool_error(
                                    failed_tool_result,
                                    protocol_violation=(
                                        "agent.failed.callId 未指向最后一个 failed ToolResult"
                                    ),
                                    reported_error=reported_error,
                                )
                            raise self._failed_tool_error(
                                failed_tool_result,
                                reported_error=reported_error,
                            )
                        if payload.get("callId") is not None:
                            raise AgentHostError(
                                "AGENT_EVENT_INVALID",
                                "agent.failed.callId 未指向 Host 已见的 failed ToolResult",
                                failed_call_id=payload.get("callId"),
                                reported_error=reported_error,
                            )
                        policy = error_policy(payload["code"])
                        message = payload["message"]
                        if policy.code not in {
                            "EXECUTION_FAILED",
                            "TIMEOUT",
                            "RATE_LIMITED",
                            "DEPENDENCY_UNAVAILABLE",
                        }:
                            # Unknown, permission, safety, contract and budget
                            # reports all normalize through the fail-closed policy.
                            message = payload["message"]
                        raise AgentHostError(
                            policy.code,
                            message,
                            failed_call_id=payload.get("callId"),
                            reported_error=reported_error,
                        )
        except TimeoutError as exc:
            await self._cancel(adapter, request.run_id)
            raise AgentHostError("TIMEOUT", "Agent 运行超时，已取消") from exc
        except AdapterError as exc:
            if failed_tool_result is not None:
                raise self._failed_tool_error(
                    failed_tool_result,
                    protocol_violation=f"Agent 子进程提前退出: {exc}",
                ) from exc
            raise AgentHostError("EXECUTION_FAILED", str(exc)) from exc

        if output is None and failed_tool_result is not None:
            raise self._failed_tool_error(failed_tool_result)
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
    def _failed_tool_error(
        payload: dict[str, Any],
        *,
        protocol_violation: str | None = None,
        reported_error: dict[str, Any] | None = None,
    ) -> AgentHostError:
        error = payload.get("error") or {}
        return AgentHostError(
            str(error.get("code", "EXECUTION_FAILED")),
            str(error.get("message", "Tool 执行失败")),
            failed_call_id=str(payload.get("callId", "")) or None,
            protocol_violation=protocol_violation,
            reported_error=reported_error,
        )

    @staticmethod
    def _validate_planned_tool_request(
        plan: AgentPlanSnapshot,
        payload: dict[str, Any],
        completed_plan_steps: set[str],
    ) -> None:
        step_id = payload.get("planStepId")
        if not step_id:
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                "结构化计划下的 tool.requested 必须关联 planStepId",
            )
        step = next((item for item in plan.steps if item.id == step_id), None)
        if step is None:
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                f"Tool request 引用了未知计划步骤 {step_id}",
            )
        if step.capabilityRef != payload.get("capabilityRef"):
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                f"计划步骤 {step_id} 的 capabilityRef 不一致",
            )
        missing = set(step.dependsOn) - completed_plan_steps
        if missing:
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                f"计划步骤 {step_id} 的依赖尚未完成: {sorted(missing)}",
            )

    @staticmethod
    def _validate_output_provenance(
        output: Any,
        provenance: Any,
        broker_payloads: dict[str, dict[str, Any]],
    ) -> None:
        if not isinstance(output, dict) or not isinstance(provenance, dict):
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                "最终输出必须提供逐字段 outputProvenance",
            )
        if set(provenance) != set(output):
            raise AgentHostError(
                "AGENT_EVENT_INVALID",
                "outputProvenance 必须恰好覆盖所有输出字段",
            )
        for output_field, source in provenance.items():
            if not isinstance(source, dict):
                raise AgentHostError(
                    "AGENT_EVENT_INVALID",
                    f"outputProvenance.{output_field} 必须是对象",
                )
            mode = source.get("mode", "deterministic")
            if mode == "generative":
                call_ids = source.get("sourceCallIds")
                digests = source.get("evidenceDigests")
                if (
                    not isinstance(call_ids, list)
                    or not call_ids
                    or not isinstance(digests, list)
                    or len(call_ids) != len(digests)
                    or not source.get("promptVersion")
                ):
                    raise AgentHostError(
                        "AGENT_EVENT_INVALID",
                        f"outputProvenance.{output_field} 缺少生成式来源集合或提示词版本",
                    )
                for call_id, digest in zip(call_ids, digests, strict=True):
                    payload = broker_payloads.get(str(call_id))
                    if (
                        payload is None
                        or payload.get("status") != "succeeded"
                        or payload.get("evidenceDigest") != digest
                    ):
                        raise AgentHostError(
                            "AGENT_EVENT_INVALID",
                            f"outputProvenance.{output_field} 引用了无效 ToolResult",
                        )
                continue
            call_id = str(source.get("callId", ""))
            payload = broker_payloads.get(call_id)
            if (
                payload is None
                or payload.get("status") != "succeeded"
                or payload.get("evidenceDigest") != source.get("evidenceDigest")
            ):
                raise AgentHostError(
                    "AGENT_EVENT_INVALID",
                    f"outputProvenance.{output_field} 引用了无效 ToolResult",
                )
            path = source.get("path")
            try:
                source_value = AgentHost._provenance_path(payload, path)
            except (KeyError, TypeError):
                raise AgentHostError(
                    "AGENT_EVENT_INVALID",
                    f"outputProvenance.{output_field} path 无效",
                ) from None
            if output[output_field] != source_value:
                raise AgentHostError(
                    "AGENT_EVENT_INVALID",
                    f"输出字段 {output_field} 与 ToolResult provenance 值不一致",
                )

    @staticmethod
    def _provenance_path(payload: dict[str, Any], path: Any) -> Any:
        if not isinstance(path, str) or not path:
            raise KeyError(path)
        current: Any = payload
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                raise KeyError(segment)
            current = current[segment]
        return current

    async def _execute_tool_request(
        self,
        request: AgentHostRequest,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        capability_ref = payload["capabilityRef"]
        try:
            descriptor = self._tool_registry.describe(capability_ref)
            signature = action_signature(
                capability_ref,
                payload["arguments"],
                effect=descriptor["effect"],
            )
            completed = next(
                (
                    item
                    for item in request.completed_actions
                    if item.get("signature") == signature
                ),
                None,
            )
            if completed is not None:
                if descriptor["effect"] != "none":
                    raise AgentHostError(
                        "ACTION_OUTCOME_UNKNOWN",
                        "已完成或结果未知的副作用动作不得由 continuation 重做",
                    )
                saved_output = completed.get("result")
                if not isinstance(saved_output, dict):
                    raise AgentHostError(
                        "ACTION_ALREADY_COMPLETED",
                        "已完成动作缺少可复用 Artifact，拒绝重做",
                    )
                evidence = {"status": "succeeded", "output": saved_output}
                return {
                    "type": "tool.result",
                    "payload": {
                        "callId": payload["callId"],
                        "capabilityRef": capability_ref,
                        "status": "succeeded",
                        "output": saved_output,
                        "evidenceDigest": evidence_digest(evidence),
                        "effect": descriptor["effect"],
                        "idempotent": descriptor["idempotent"],
                        "fixturePolicy": descriptor["fixturePolicy"],
                        "actionSignature": signature,
                        "reusedFromLedger": True,
                        **self._tool_result_metadata(payload),
                    },
                }
            attempts = 0
            while True:
                attempts += 1
                try:
                    output = await self._tool_registry.execute(
                        capability_ref,
                        ToolExecutionContext(
                            run_id=request.run_id,
                            call_id=payload["callId"],
                            working_directory=request.working_directory,
                        ),
                        payload["arguments"],
                    )
                    break
                except ToolBrokerError as exc:
                    policy = error_policy(exc.code)
                    if (
                        not policy.retryable
                        or descriptor["effect"] != "none"
                        or attempts >= policy.max_attempts
                    ):
                        raise
                    await asyncio.sleep(policy.backoff_seconds)
            evidence = {"status": "succeeded", "output": output}
            return {
                "type": "tool.result",
                "payload": {
                    "callId": payload["callId"],
                    "capabilityRef": capability_ref,
                    "status": "succeeded",
                    "output": output,
                    "evidenceDigest": evidence_digest(evidence),
                    "effect": descriptor["effect"],
                    "idempotent": descriptor["idempotent"],
                    "fixturePolicy": descriptor["fixturePolicy"],
                    "actionSignature": signature,
                    "attempts": attempts,
                    **self._tool_result_metadata(
                        payload,
                        self._tool_registry.distillation_evidence(
                            capability_ref, output
                        ),
                    ),
                },
            }
        except ToolBrokerError as exc:
            descriptor = self._tool_registry.describe(capability_ref)
            signature = action_signature(
                capability_ref,
                payload["arguments"],
                effect=descriptor["effect"],
            )
            policy = error_policy(exc.code)
            error = {
                "code": policy.code,
                "message": exc.message,
                "retryable": policy.retryable,
            }
            evidence = {"status": "failed", "error": error}
            return {
                "type": "tool.result",
                "payload": {
                    "callId": payload["callId"],
                    "capabilityRef": capability_ref,
                    "status": "failed",
                    "error": error,
                    "evidenceDigest": evidence_digest(evidence),
                    "effect": descriptor["effect"],
                    "idempotent": descriptor["idempotent"],
                    "fixturePolicy": descriptor["fixturePolicy"],
                    "actionSignature": signature,
                    "attempts": locals().get("attempts", 1),
                    **self._tool_result_metadata(payload),
                },
            }

    @staticmethod
    def _tool_result_metadata(
        payload: dict[str, Any],
        adapter_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        keys = (
            "action",
            "actionKind",
            "argumentBindings",
            "effect",
            "idempotent",
            "observations",
            "outputSchema",
            "planStepId",
            "replayability",
        )
        metadata = {key: payload[key] for key in keys if key in payload}
        observations = dict(metadata.get("observations") or {})
        observations.update(adapter_evidence or {})
        observations["resultSource"] = "host_tool_broker"
        metadata["observations"] = observations
        metadata["argumentsDigest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                payload.get("arguments", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return metadata

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
        elif event_type == "tool.result":
            kind = completed.get("actionKind") or "tool"
            capability = completed.get("capabilityRef", "")
        else:
            kind = "tool"
            capability = completed.get("tool", "")
        action: dict[str, Any] = {
            "callId": completed["callId"],
            "kind": completed.get("actionKind") or kind,
            "capability": capability,
            "action": completed.get("action") or started.get("action"),
            "arguments": dict(started.get("arguments", {})),
            "result": completed.get("output", completed.get("result", {})),
        }
        if event_type == "tool.result" and kind == "browser":
            action["arguments"].pop("pageContract", None)
        if event_type == "tool.result":
            action["status"] = completed.get("status")
            action["evidenceDigest"] = completed.get("evidenceDigest")
        # 透传固化证据（如果 Runtime 提供）。
        for key in ("capabilityRef", "outputSchema", "replayability", "effect", "idempotent"):
            if key in completed:
                action[key] = completed[key]
            elif key in started:
                action[key] = started[key]
        # 显式 per-argument provenance：Runtime 为每个参数声明来源
        # （user_input / prior_output(actionId,path) / artifact(id,path) / constant），
        # 不再靠“值是否等于 prompt”猜测。见 build_action_ir。
        bindings = completed.get("argumentBindings") or started.get("argumentBindings")
        if isinstance(bindings, dict):
            action["argumentBindings"] = bindings
        observations = completed.get("observations") or started.get("observations")
        if isinstance(observations, dict):
            action["observations"] = observations
        return action

    @staticmethod
    async def _await_cancel(cancellation: CancellationCheck) -> bool:
        """轮询取消信号；置位即返回 True（与事件流竞争，及时终止挂起子进程）。"""

        while True:
            if await cancellation():
                return True
            await asyncio.sleep(0.1)

    @staticmethod
    async def _cancel(adapter: TraeXSubprocessAdapter, run_id: str) -> None:
        try:
            await adapter.cancel(run_id)
        except Exception:  # noqa: BLE001 - 取消失败不应掩盖原始终态。
            pass


def action_signature(
    capability_ref: str,
    arguments: dict[str, Any],
    *,
    effect: str,
) -> str:
    payload = {
        "capabilityRef": capability_ref,
        "argumentsDigest": "sha256:"
        + hashlib.sha256(
            json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "effect": effect,
        "idempotencyKey": arguments.get("idempotencyKey"),
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
