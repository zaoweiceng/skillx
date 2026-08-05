from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

from app.application.security import redact

# 004-B / 005-A：通用 Agent 事件契约。
#
# 不信任 TraeX 子进程是否遵守命令行参数或输出规范。所有 raw 事件必须通过判别联合
# 模型校验：非对象 JSON、未知 type、非法 Token、缺失 callId/tool、超大 payload 一律
# fail closed，并由调用方持久化 ``AGENT_EVENT_INVALID``。Host 据此独立证明权限与预算
# 没有越界，而不是相信 Runtime 自我约束。
#
# 005-A：validator 同时产出 runtime event（内部完整 payload，仅在进程内使用）与
# audit event（写入数据库/API 的脱敏摘要——只含 callId、tool、字段名、结果摘要、
# hash、字节数与 Artifact 引用）。Tool 原始参数/结果绝不进入数据库或事件 API。


def _canonical_bytes(value: Any) -> int:
    """canonical JSON 编码后的 UTF-8 字节数（而非 str 长度），用于真实限流。"""

    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(canonical.encode())


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def summarize_value(value: Any, *, max_len: int = 120) -> dict[str, Any]:
    """把任意 Tool 参数/结果压成脱敏摘要：字段名、类型、hash、字节数、截断预览。

    不保存原值。字符串只保留脱敏后的短预览；对象只保留（脱敏后的）键名。
    """

    redacted = redact(value)
    summary: dict[str, Any] = {
        "digest": _digest(value),
        "bytes": _canonical_bytes(value),
    }
    if isinstance(value, dict):
        summary["type"] = "object"
        summary["fields"] = sorted(str(key) for key in value)
    elif isinstance(value, list):
        summary["type"] = "array"
        summary["length"] = len(value)
    elif isinstance(value, str):
        summary["type"] = "string"
        preview = redacted if isinstance(redacted, str) else str(redacted)
        summary["preview"] = preview[:max_len]
    else:
        summary["type"] = type(value).__name__
        summary["preview"] = str(redacted)[:max_len]
    return summary


class AgentEventError(Exception):
    """事件不符合契约的确定性失败，映射为 ``AGENT_EVENT_INVALID``。"""


class _PayloadBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class PlanPayload(_PayloadBase):
    summary: str = ""
    steps: StrictInt | None = None


class StepCompletedPayload(_PayloadBase):
    step: StrictInt | None = None
    # StrictInt：字符串/浮点 Token 视为畸形事件而非静默 int() 强转。
    inputTokens: StrictInt = 0
    outputTokens: StrictInt = 0


class CompletedPayload(_PayloadBase):
    steps: StrictInt | None = None
    totalTokens: StrictInt | None = None
    output: dict[str, Any] | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    outputProvenance: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ToolPayload(_PayloadBase):
    # Tool 事件必须携带 callId 与 tool，用于 started/completed 一一配对与权限核对。
    callId: str = Field(min_length=1)
    tool: str = Field(min_length=1)


class BrowserActionPayload(_PayloadBase):
    # 通用 Browser/BUA 动作事件：与 Tool 一样按 callId 配对，capability 作为权限键。
    callId: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    action: str = Field(min_length=1)


class ToolRequestPayload(_PayloadBase):
    callId: str = Field(min_length=1)
    capabilityRef: str = Field(min_length=1)
    arguments: dict[str, Any]


class ToolResultPayload(_PayloadBase):
    callId: str = Field(min_length=1)
    capabilityRef: str = Field(min_length=1)
    status: Literal["succeeded", "failed"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    evidenceDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class LogPayload(_PayloadBase):
    message: str = ""


class AgentPlanEvent(BaseModel):
    type: Literal["agent.plan"]
    payload: PlanPayload


class AgentStepEvent(BaseModel):
    type: Literal["agent.step.completed"]
    payload: StepCompletedPayload


class AgentCompletedEvent(BaseModel):
    type: Literal["agent.completed"]
    payload: CompletedPayload


class ToolStartedEvent(BaseModel):
    type: Literal["tool.call.started"]
    payload: ToolPayload


class ToolCompletedEvent(BaseModel):
    type: Literal["tool.call.completed"]
    payload: ToolPayload


class BrowserActionStartedEvent(BaseModel):
    type: Literal["browser.action.started"]
    payload: BrowserActionPayload


class BrowserActionCompletedEvent(BaseModel):
    type: Literal["browser.action.completed"]
    payload: BrowserActionPayload


class ToolRequestedEvent(BaseModel):
    type: Literal["tool.requested"]
    payload: ToolRequestPayload


class ToolResultEvent(BaseModel):
    type: Literal["tool.result"]
    payload: ToolResultPayload


class LogEvent(BaseModel):
    type: Literal["log.emitted"]
    payload: LogPayload


AgentEvent = Annotated[
    AgentPlanEvent
    | AgentStepEvent
    | AgentCompletedEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | BrowserActionStartedEvent
    | BrowserActionCompletedEvent
    | ToolRequestedEvent
    | ToolResultEvent
    | LogEvent,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(AgentEvent)

# 事件类型 → (started 后缀, completed 后缀, 权限字段, 动作 kind)。
# tool.* 与 browser.action.* 共享同一套 callId 配对与权限核对逻辑。
_CALL_STARTED = {
    "tool.call.started": "tool",
    "browser.action.started": "capability",
    "tool.requested": "capabilityRef",
}
_CALL_COMPLETED = {
    "tool.call.completed": "tool",
    "browser.action.completed": "capability",
    "tool.result": "capabilityRef",
}


class AgentEventValidator:
    """跨事件流的有状态校验器。

    职责：
    - 逐条判别联合校验（未知 type/非对象 payload/非法 Token fail closed）；
    - payload 大小上限（canonical JSON UTF-8 bytes）；
    - Tool/Browser 动作权限核对（``key ∈ allowed``），并要求 started/completed 按 callId 配对；
    - ``agent.completed`` 终态只允许出现一次；
    - 返回 ``(runtime_event, audit_event)``：前者含完整 payload 供进程内使用，后者只含
      脱敏摘要供数据库/API 持久化。
    """

    def __init__(self, allowed: set[str], *, max_payload_bytes: int) -> None:
        self._allowed = allowed
        self._max_payload_bytes = max_payload_bytes
        self._open_calls: dict[str, str] = {}
        self._completed = False

    def validate(self, raw: Any) -> dict[str, Any]:
        """向后兼容入口：只返回 runtime event（完整 payload）。"""

        runtime, _ = self.validate_pair(raw)
        return runtime

    def validate_pair(self, raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._completed:
            raise AgentEventError("agent.completed 之后不得再出现事件")
        if not isinstance(raw, dict):
            raise AgentEventError("Agent 事件必须为 JSON 对象")
        if "payload" in raw and not isinstance(raw["payload"], dict):
            raise AgentEventError("Agent 事件 payload 必须为对象")
        try:
            event = _ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise AgentEventError(f"Agent 事件不符合契约: {exc.errors()[0]['msg']}") from exc
        model = event.model_dump()
        payload = model["payload"]
        # 用 canonical JSON UTF-8 字节数限流：中文等多字节内容不会绕过真实上限。
        if _canonical_bytes(payload) > self._max_payload_bytes:
            raise AgentEventError("Agent 事件 payload 超过大小上限")
        event_type = model["type"]
        if event_type in _CALL_STARTED:
            self._on_call_started(event_type, payload)
        elif event_type in _CALL_COMPLETED:
            self._on_call_completed(event_type, payload)
        elif event_type == "agent.completed":
            if self._open_calls:
                raise AgentEventError("存在未闭合的 Tool/Browser 调用，无法完成")
            self._completed = True
        runtime_event = {"type": event_type, "payload": payload}
        return runtime_event, self._audit_event(event_type, payload)

    @staticmethod
    def _audit_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """公开审计事件：只含 callId/能力键/字段摘要/结果摘要，绝不含原始参数/结果。"""

        key_field = _CALL_STARTED.get(event_type) or _CALL_COMPLETED.get(event_type)
        if key_field is not None:
            audit: dict[str, Any] = {
                "callId": payload["callId"],
                key_field: payload[key_field],
            }
            if "action" in payload:
                audit["action"] = payload["action"]
            if "arguments" in payload:
                audit["arguments"] = summarize_value(payload["arguments"])
            if "result" in payload:
                audit["result"] = summarize_value(payload["result"])
            if "output" in payload:
                audit["output"] = summarize_value(payload["output"])
            if "error" in payload and payload["error"] is not None:
                audit["error"] = summarize_value(payload["error"])
            if "status" in payload:
                audit["status"] = payload["status"]
            if "evidenceDigest" in payload:
                audit["evidenceDigest"] = payload["evidenceDigest"]
            if payload.get("reusedFromLedger") is True:
                audit["reusedFromLedger"] = True
            return {"type": event_type, "payload": audit}
        if event_type == "agent.completed":
            # 完成事件只保留步数/Token 与输出摘要，不落原始 output。
            audit = {
                "steps": payload.get("steps"),
                "totalTokens": payload.get("totalTokens"),
            }
            if payload.get("output") is not None:
                audit["output"] = summarize_value(payload["output"])
            return {"type": event_type, "payload": audit}
        if event_type == "agent.step.completed":
            return {
                "type": event_type,
                "payload": {
                    "step": payload.get("step"),
                    "inputTokens": payload.get("inputTokens", 0),
                    "outputTokens": payload.get("outputTokens", 0),
                },
            }
        if event_type == "agent.plan":
            return {"type": event_type, "payload": {"summary": payload.get("summary", "")}}
        # log.emitted：只保留脱敏后的短消息。
        return {"type": event_type, "payload": summarize_value(payload)}

    def _authorize(self, key: str) -> None:
        if key not in self._allowed:
            raise PermissionError(key)

    def _on_call_started(self, event_type: str, payload: dict[str, Any]) -> None:
        key = payload[_CALL_STARTED[event_type]]
        self._authorize(key)
        call_id = payload["callId"]
        if call_id in self._open_calls:
            raise AgentEventError(f"重复的调用 callId {call_id}")
        self._open_calls[call_id] = key

    def _on_call_completed(self, event_type: str, payload: dict[str, Any]) -> None:
        key = payload[_CALL_COMPLETED[event_type]]
        self._authorize(key)
        call_id = payload["callId"]
        opened = self._open_calls.pop(call_id, None)
        if opened is None:
            raise AgentEventError(f"完成事件缺少匹配的 started: {call_id}")
        if opened != key:
            raise AgentEventError(f"callId {call_id} 的能力键不一致")

    @property
    def completed(self) -> bool:
        return self._completed
