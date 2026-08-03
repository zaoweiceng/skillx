from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

# 004-B：通用 Agent 事件契约。
#
# 不信任 TraeX 子进程是否遵守命令行参数或输出规范。所有 raw 事件必须通过判别联合
# 模型校验：非对象 JSON、未知 type、非法 Token、缺失 callId/tool、超大 payload 一律
# fail closed，并由调用方持久化 ``AGENT_EVENT_INVALID``。Host 据此独立证明权限与预算
# 没有越界，而不是相信 Runtime 自我约束。


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


class ToolPayload(_PayloadBase):
    # Tool 事件必须携带 callId 与 tool，用于 started/completed 一一配对与权限核对。
    callId: str = Field(min_length=1)
    tool: str = Field(min_length=1)


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


class LogEvent(BaseModel):
    type: Literal["log.emitted"]
    payload: LogPayload


AgentEvent = Annotated[
    AgentPlanEvent
    | AgentStepEvent
    | AgentCompletedEvent
    | ToolStartedEvent
    | ToolCompletedEvent
    | LogEvent,
    Field(discriminator="type"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(AgentEvent)


class AgentEventValidator:
    """跨事件流的有状态校验器。

    职责：
    - 逐条判别联合校验（未知 type/非对象 payload/非法 Token fail closed）；
    - payload 大小上限；
    - Tool 权限核对（``tool ∈ allowed``），并要求 started/completed 按 callId 配对；
    - ``agent.completed`` 终态只允许出现一次。
    """

    def __init__(self, allowed: set[str], *, max_payload_bytes: int) -> None:
        self._allowed = allowed
        self._max_payload_bytes = max_payload_bytes
        self._open_calls: dict[str, str] = {}
        self._completed = False

    def validate(self, raw: Any) -> dict[str, Any]:
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
        if len(str(payload)) > self._max_payload_bytes:
            raise AgentEventError("Agent 事件 payload 超过大小上限")
        event_type = model["type"]
        if event_type == "tool.call.started":
            self._on_tool_started(payload)
        elif event_type == "tool.call.completed":
            self._on_tool_completed(payload)
        elif event_type == "agent.completed":
            if self._open_calls:
                raise AgentEventError("存在未闭合的 Tool 调用，无法完成")
            self._completed = True
        return {"type": event_type, "payload": payload}

    def _authorize_tool(self, tool: str) -> None:
        if tool not in self._allowed:
            raise PermissionError(tool)

    def _on_tool_started(self, payload: dict[str, Any]) -> None:
        tool = payload["tool"]
        self._authorize_tool(tool)
        call_id = payload["callId"]
        if call_id in self._open_calls:
            raise AgentEventError(f"重复的 Tool callId {call_id}")
        self._open_calls[call_id] = tool

    def _on_tool_completed(self, payload: dict[str, Any]) -> None:
        tool = payload["tool"]
        self._authorize_tool(tool)
        call_id = payload["callId"]
        opened = self._open_calls.pop(call_id, None)
        if opened is None:
            raise AgentEventError(f"Tool 完成事件缺少匹配的 started: {call_id}")
        if opened != tool:
            raise AgentEventError(f"Tool callId {call_id} 的 tool 名称不一致")

    @property
    def completed(self) -> bool:
        return self._completed
