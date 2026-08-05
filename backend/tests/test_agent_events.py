import pytest

from app.application.agent_events import AgentEventError, AgentEventValidator


def _validator() -> AgentEventValidator:
    return AgentEventValidator({"search.web"}, max_payload_bytes=65536)


def test_accepts_well_formed_plan_and_step() -> None:
    validator = _validator()
    assert validator.validate({"type": "agent.plan", "payload": {"summary": "计划"}})["type"] == (
        "agent.plan"
    )
    step = validator.validate(
        {
            "type": "agent.step.completed",
            "payload": {"step": 1, "inputTokens": 3, "outputTokens": 2},
        }
    )
    assert step["payload"]["inputTokens"] == 3


def test_rejects_non_object_event() -> None:
    with pytest.raises(AgentEventError):
        _validator().validate(["not", "an", "object"])


def test_rejects_unknown_event_type() -> None:
    with pytest.raises(AgentEventError):
        _validator().validate({"type": "mystery.event", "payload": {}})


def test_rejects_non_object_payload() -> None:
    with pytest.raises(AgentEventError):
        _validator().validate({"type": "agent.plan", "payload": "oops"})


def test_rejects_illegal_token_field() -> None:
    with pytest.raises(AgentEventError):
        _validator().validate(
            {"type": "agent.step.completed", "payload": {"inputTokens": "lots"}}
        )


def test_rejects_oversized_payload() -> None:
    validator = AgentEventValidator({"search.web"}, max_payload_bytes=16)
    with pytest.raises(AgentEventError):
        validator.validate({"type": "agent.plan", "payload": {"summary": "x" * 1000}})


def test_unauthorized_tool_raises_permission_error() -> None:
    validator = _validator()
    with pytest.raises(PermissionError):
        validator.validate(
            {"type": "tool.call.started", "payload": {"callId": "c1", "tool": "banned.tool"}}
        )


def test_tool_events_require_call_id() -> None:
    with pytest.raises(AgentEventError):
        _validator().validate(
            {"type": "tool.call.started", "payload": {"tool": "search.web"}}
        )


def test_tool_started_and_completed_must_pair() -> None:
    validator = _validator()
    validator.validate(
        {"type": "tool.call.started", "payload": {"callId": "c1", "tool": "search.web"}}
    )
    # 完成事件 callId 不匹配 → fail closed。
    with pytest.raises(AgentEventError):
        validator.validate(
            {"type": "tool.call.completed", "payload": {"callId": "c2", "tool": "search.web"}}
        )


def test_completed_only_once() -> None:
    validator = _validator()
    validator.validate({"type": "agent.completed", "payload": {"steps": 1, "output": {}}})
    with pytest.raises(AgentEventError):
        validator.validate({"type": "agent.plan", "payload": {"summary": "again"}})


def test_agent_failed_is_structured_terminal() -> None:
    validator = _validator()
    failed = validator.validate(
        {
            "type": "agent.failed",
            "payload": {
                "code": "DEPENDENCY_UNAVAILABLE",
                "message": "upstream unavailable",
                "retryable": True,
                "callId": "call-1",
            },
        }
    )
    assert failed["payload"]["retryable"] is True
    with pytest.raises(AgentEventError):
        validator.validate({"type": "agent.plan", "payload": {}})


def test_completed_requires_all_tool_calls_closed() -> None:
    validator = _validator()
    validator.validate(
        {"type": "tool.call.started", "payload": {"callId": "c1", "tool": "search.web"}}
    )
    with pytest.raises(AgentEventError):
        validator.validate({"type": "agent.completed", "payload": {"steps": 1, "output": {}}})
