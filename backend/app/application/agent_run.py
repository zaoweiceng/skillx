from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.infrastructure.database import (
    Conversation,
    ConversationRun,
    ConversationRunEvent,
)
from app.runtime.adapters import AdapterError, AgentRunRequest, TraeXSubprocessAdapter

# 通用 Agent 运行的规范化事件类型。未知类型或格式不符的 Tool 事件默认拒绝，
# 不得绕过 allowlist 检查。
ALLOWED_AGENT_EVENT_TYPES = frozenset(
    {
        "agent.plan",
        "agent.step.completed",
        "agent.completed",
        "tool.call.started",
        "tool.call.completed",
        "log.emitted",
    }
)


class AgentRunError(Exception):
    """AgentRunService 归一化过程中的确定性失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class AgentToolViolation(AgentRunError):
    """Agent 调用了 allowedCapabilities 之外的工具或发出了非法事件。"""


@dataclass
class AgentRunConfig:
    """一次受限通用 Agent 运行的配置。"""

    output_schema: dict[str, Any]
    max_steps: int = 6
    max_total_tokens: int = 30000
    allowed_capabilities: tuple[str, ...] = ()
    max_duration_seconds: int = 120

    @staticmethod
    def default() -> AgentRunConfig:
        return AgentRunConfig(
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            },
            allowed_capabilities=("search.web",),
        )


async def _append_event(
    session: AsyncSession,
    conversation_run_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        ConversationRunEvent(
            id=str(uuid4()),
            conversation_run_id=conversation_run_id,
            seq=seq,
            type=event_type,
            payload_json=payload,
        )
    )
    await session.flush()


class AgentRunService:
    """唯一负责创建 ``ConversationRun(run_type="agent_run")`` 的服务。

    职责：启动受限 TraeX 子进程、归一化并持久化事件、执行 allowlist 与输出 Schema
    校验、处理超时/取消，并在成功时写入结构化 output、规范化 steps、Tool 事件摘要、
    Token/步数与来源 Artifact 摘要。任何其它路径都不得伪造 agent_run。
    """

    def __init__(self, adapter: TraeXSubprocessAdapter | None = None) -> None:
        self._adapter = adapter

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

    async def start_agent_run(
        self,
        session: AsyncSession,
        conversation: Conversation,
        content: str,
        *,
        config: AgentRunConfig | None = None,
        prompt: str | None = None,
    ) -> ConversationRun:
        config = config or AgentRunConfig.default()
        conversation_run = ConversationRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            status="running",
            run_type="agent_run",
            input_json={"message": content},
            output_json={},
            trajectory_json={},
        )
        session.add(conversation_run)
        await session.flush()
        seq = 1
        await _append_event(
            session,
            conversation_run.id,
            seq,
            "agent.run.created",
            {"conversationRunId": conversation_run.id, "input": {"message": content}},
        )

        try:
            adapter = self._resolve_adapter()
        except AgentRunError as exc:
            await self._finalize_failure(session, conversation_run, seq + 1, exc)
            return conversation_run

        allowed = set(config.allowed_capabilities)
        request = AgentRunRequest(
            prompt=(prompt if prompt is not None else content),
            working_directory=(
                get_settings().artifact_root / "conversation-agents" / conversation_run.id
            ),
            allowed_tools=list(config.allowed_capabilities),
            max_steps=config.max_steps,
            max_tokens=config.max_total_tokens,
        )

        normalized_events: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        output: dict[str, Any] | None = None
        total_tokens = 0
        step_count = 0

        try:
            async with asyncio.timeout(config.max_duration_seconds):
                async for raw in adapter.events(conversation_run.id, request):
                    event = self._normalize(raw, allowed)
                    seq += 1
                    await _append_event(
                        session,
                        conversation_run.id,
                        seq,
                        event["type"],
                        event["payload"],
                    )
                    normalized_events.append(event)
                    if event["type"].startswith("tool."):
                        tool_events.append(event)
                    if event["type"] == "agent.step.completed":
                        step_count += 1
                        total_tokens += int(event["payload"].get("inputTokens", 0)) + int(
                            event["payload"].get("outputTokens", 0)
                        )
                    if event["type"] == "agent.completed":
                        output = event["payload"].get("output")
                        total_tokens = int(event["payload"].get("totalTokens", total_tokens))
            if output is None:
                raise AgentRunError("EXECUTION_FAILED", "Agent 未返回结构化输出")
            errors = list(Draft202012Validator(config.output_schema).iter_errors(output))
            if errors:
                raise AgentRunError(
                    "CONTRACT_VIOLATION",
                    f"Agent 输出不符合 outputSchema: {errors[0].message}",
                )
        except AgentRunError as exc:
            await self._finalize_failure(session, conversation_run, seq + 1, exc)
            return conversation_run
        except TimeoutError:
            await self._cancel_adapter(adapter, conversation_run.id)
            await self._finalize_failure(
                session,
                conversation_run,
                seq + 1,
                AgentRunError("TIMEOUT", "Agent 运行超时，已取消"),
            )
            return conversation_run
        except AdapterError as exc:
            await self._finalize_failure(
                session,
                conversation_run,
                seq + 1,
                AgentRunError("EXECUTION_FAILED", str(exc)),
            )
            return conversation_run

        trajectory = self._build_trajectory(
            config,
            request.prompt,
            step_count,
        )
        summary = {
            "totalTokens": total_tokens,
            "steps": step_count,
            "toolEvents": [
                {"type": item["type"], "tool": item["payload"].get("tool")}
                for item in tool_events
            ],
        }
        conversation_run.status = "succeeded"
        conversation_run.output_json = output
        conversation_run.trajectory_json = trajectory
        seq += 1
        await _append_event(
            session,
            conversation_run.id,
            seq,
            "agent.run.succeeded",
            {"conversationRunId": conversation_run.id, "summary": summary},
        )
        return conversation_run

    def _normalize(self, raw: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        event_type = raw.get("type", "log.emitted")
        payload = raw.get("payload", {})
        if not isinstance(payload, dict):
            raise AgentToolViolation(
                "AGENT_EVENT_INVALID", f"事件 {event_type} 的 payload 必须为对象"
            )
        if event_type not in ALLOWED_AGENT_EVENT_TYPES:
            raise AgentToolViolation(
                "AGENT_EVENT_INVALID", f"未知 Agent 事件类型 {event_type}，已拒绝"
            )
        if event_type.startswith("tool."):
            tool_name = payload.get("tool")
            if not isinstance(tool_name, str) or not tool_name:
                raise AgentToolViolation(
                    "AGENT_EVENT_INVALID", f"Tool 事件 {event_type} 缺少合法 tool 字段"
                )
            if tool_name not in allowed:
                raise AgentToolViolation(
                    "PERMISSION_DENIED",
                    f"Agent 调用了未授权工具 {tool_name}，不在 allowedCapabilities 内",
                )
        return {"type": event_type, "payload": payload}

    @staticmethod
    def _build_trajectory(
        config: AgentRunConfig,
        prompt: str,
        step_count: int,
    ) -> dict[str, Any]:
        """把一次成功的 Agent 运行归一化为可固化的轨迹。

        固化后的 Workflow 用一个受限 Agent 节点复现该探索：新输入通过 ``${input.message}``
        注入，其余限制（outputSchema/maxSteps/allowedCapabilities）来自本次运行配置。
        """

        return {
            "runType": "agent_run",
            "name": "conversation-agent-draft",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            "outputSchema": config.output_schema,
            "steps": [
                {
                    "id": "agent",
                    "kind": "agent",
                    "displayName": "受限通用 Agent",
                    "input": {"message": "${input.message}"},
                    "config": {
                        "outputSchema": config.output_schema,
                        "maxSteps": config.max_steps,
                        "maxTotalTokens": config.max_total_tokens,
                        "allowedCapabilities": list(config.allowed_capabilities),
                        "prompt": prompt,
                    },
                }
            ],
            "outputMapping": {
                key: f"${{agent.output.{key}}}"
                for key in config.output_schema.get("properties", {})
            },
            "originalModelCalls": max(step_count, 1),
        }

    @staticmethod
    async def _cancel_adapter(adapter: TraeXSubprocessAdapter, run_id: str) -> None:
        try:
            await adapter.cancel(run_id)
        except Exception:  # noqa: BLE001 - 取消失败不应掩盖原始超时。
            pass

    @staticmethod
    async def _finalize_failure(
        session: AsyncSession,
        conversation_run: ConversationRun,
        seq: int,
        error: AgentRunError,
    ) -> None:
        conversation_run.status = "failed"
        conversation_run.error_json = {
            "code": error.code,
            "message": error.message,
            "retryable": error.code in {"TIMEOUT", "EXECUTION_FAILED"},
        }
        await _append_event(
            session,
            conversation_run.id,
            seq,
            "agent.run.failed",
            {
                "conversationRunId": conversation_run.id,
                "error": conversation_run.error_json,
            },
        )


def summarize_run(conversation_run: ConversationRun) -> dict[str, Any]:
    """对话运行的 API 摘要视图。"""

    return {
        "id": conversation_run.id,
        "conversationId": conversation_run.conversation_id,
        "status": conversation_run.status,
        "runType": conversation_run.run_type,
        "input": conversation_run.input_json,
        "output": conversation_run.output_json,
        "error": conversation_run.error_json,
    }
