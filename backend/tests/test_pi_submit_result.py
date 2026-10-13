"""Rejected SDK submissions must reach Host validation, even at one step."""
import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    AgentOutputValidationError,
)
from app.application.runtime_config import agent_runtime_config
from app.config import get_settings


def test_extra_field_feedback_names_only_invalid_keys():
    from app.application.agent_host import output_schema_feedback

    feedback = output_schema_feedback({
        "type": "object", "properties": {"reply": {"type": "string"}},
        "patternProperties": {"^extension_": {}}, "additionalProperties": False,
    }, {"reply": "ok", "extension_allowed": 1, "unexpected": "private value"})
    assert feedback == '$: additionalProperties 禁止额外字段 ["unexpected"]'


@pytest.mark.parametrize("copies", [1, 2, 3])
@pytest.mark.parametrize("task_mode", [False, True])
@pytest.mark.parametrize(("candidate", "feedback"), [
    ({"reply": "ok"}, None),
    ({"reply": "ok", "completionCriteria_extra": "private value"}, "completionCriteria_extra"),
    ({}, 'required ["reply"]'),
    ({"reply": {"nested": "private value"}}, 'reply: type "string"'),
])
async def test_invalid_submission_has_actionable_feedback_and_usage(
    monkeypatch, tmp_path, candidate, feedback, copies, task_mode, malformed=None,
):
    from app.application.task_contracts import task_decision_schema

    if task_mode:
        candidate = {("answer" if key == "reply" else key): value
                     for key, value in candidate.items()}
        feedback = feedback.replace("reply", "answer") if feedback else None
    expected = ({"summary": "任务完成", "completionCriteria": ["完成用户目标"],
                 **candidate, "action": "complete"} if task_mode else candidate)
    requests = []

    async def respond(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = int(next(line.split(b":", 1)[1] for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:")))
        requests.append(json.loads(await reader.readexactly(length)))
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{
                "index": i, "id": f"submit-{i}", "type": "function", "function": {
                    "name": "task_complete" if task_mode else "submit_result",
                    "arguments": malformed if malformed is not None and i == copies - 1 else json.dumps(
                        {"reply": "conflicting"} if copies == 3 and i == 2 else candidate),
                },
            } for i in range(copies)]}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        body = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n").encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "pi")
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(settings, "agent_endpoint", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1")
    monkeypatch.setattr(settings, "agent_model", "test-model")
    monkeypatch.setenv(settings.agent_api_key_env, "test-local-key")
    runtime = replace(agent_runtime_config(), model_options={"structuredMode": "json_schema"})
    try:
        request = AgentHostRequest(
            run_id=str(uuid4()), prompt="Return a reply", max_steps=1,
            working_directory=tmp_path, requested_capabilities=(),
            output_schema=task_decision_schema([]) if task_mode else {
                           "type": "object", "properties": {"reply": {"type": "string"}},
                           "required": ["reply"], "additionalProperties": False},
        )
        if malformed is not None:
            with pytest.raises(AgentHostError, match="工具参数不是合法 JSON") as raised:
                await AgentHost(runtime=runtime).execute(request)
            assert raised.value.code == "MODEL_MALFORMED_OUTPUT"
            assert raised.value.total_tokens == 15
            assert raised.value.request_dispatched
            diagnostic = raised.value.reported_error["outputParseError"]
            assert diagnostic["rawArguments"] == malformed
            assert diagnostic["kind"] == "invalid_json"
            assert diagnostic["truncated"] is False
            assert diagnostic["stopReason"] == "toolUse"
        elif copies == 3:
            with pytest.raises(AgentHostError, match="结果提交冲突"):
                await AgentHost(runtime=runtime).execute(request)
        elif feedback is None:
            result = await AgentHost(runtime=runtime).execute(request)
            assert result.output == expected
            assert result.total_tokens == 15
        else:
            with pytest.raises(AgentOutputValidationError) as raised:
                await AgentHost(runtime=runtime).execute(request)
            assert raised.value.code == "CONTRACT_VIOLATION"
            assert raised.value.candidate == expected
            assert feedback in str(raised.value)
            assert "private value" not in str(raised.value)
            assert raised.value.total_tokens == 15
            assert raised.value.request_dispatched
        assert len(requests) == 1  # Repair belongs to the bounded Host task loop.
        if task_mode:
            assert requests[0]["tool_choice"] == "required"
            completion = next(tool["function"] for tool in requests[0]["tools"]
                              if tool["function"]["name"] == "task_complete")
            assert "calls" not in completion["parameters"]["properties"]
            assert "completionCriteria" not in completion["parameters"]["properties"]
            assert "TaskCall" not in completion["parameters"].get("$defs", {})
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("copies", [1, 2])
@pytest.mark.parametrize("task_mode", [False, True])
@pytest.mark.parametrize("malformed", [
    '{"reply">"ok"}</invoke></function_calls>',
    '{"reply":"ok"',  # SDK partial JSON recovery must not accept truncated data.
])
async def test_malformed_submission_rejected_before_schema_or_conflict(
    monkeypatch, tmp_path, copies, task_mode, malformed,
):
    await test_invalid_submission_has_actionable_feedback_and_usage(
        monkeypatch, tmp_path, {"reply": "ok"}, None, copies, task_mode,
        malformed=malformed,
    )
