"""Real Pi SDK against a deterministic local OpenAI protocol server."""

import asyncio
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.agent_host import AgentHost, AgentHostRequest
from app.config import get_settings
from app.runtime.agent_registry import AgentAdapterRegistry

BRIDGE = Path(__file__).resolve().parents[2] / "bridges/pi/bridge.mjs"


def test_registry_rejects_duplicate_registration() -> None:
    registry = AgentAdapterRegistry()
    registry.register("custom", lambda runtime: None)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("custom", lambda runtime: None)


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required for Pi SDK")
@pytest.mark.parametrize("with_tool", [False, True])
@pytest.mark.parametrize("effort", [None, "high"])
async def test_pi_sdk_returns_validated_output(monkeypatch, tmp_path, with_tool, effort) -> None:
    received = []

    async def respond(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = int(
            next(
                line.split(b":", 1)[1]
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        received.append(json.loads(await reader.readexactly(length)))
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": '{"reply":"ok"}'},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ]
        if with_tool and len(received) == 1:
            chunks[0]["choices"][0]["delta"] = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-local",
                        "type": "function",
                        "function": {"name": "host_tool_0", "arguments": '{"key":"q1"}'},
                    }
                ],
            }
            chunks[1]["choices"][0]["finish_reason"] = "tool_calls"
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        body += "data: [DONE]\n\n"
        encoded = body.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(encoded)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + encoded
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "pi")
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(settings, "agent_endpoint", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(settings, "agent_model", "test-model")
    monkeypatch.setenv(settings.agent_api_key_env, "test-local-key")
    (tmp_path / "AGENTS.md").write_text("UNRELATED_PRIVATE_CONTEXT_NEVER_LOAD")
    from dataclasses import replace

    from app.application.runtime_config import agent_runtime_config
    from app.runtime.agent_registry import default_agent_registry

    runtime = replace(agent_runtime_config(), reasoning_effort=effort)
    try:
        result = await AgentHost(adapter=default_agent_registry().create(runtime)).execute(
            AgentHostRequest(
                run_id=str(uuid4()),
                prompt="Return reply ok",
                requested_capabilities=("lookup.table",) if with_tool else (),
                working_directory=tmp_path,
                output_schema={
                    "type": "object",
                    "properties": {"reply": {"type": "string"}},
                    "required": ["reply"],
                    "additionalProperties": False,
                },
            )
        )
        assert result.output == {"reply": "ok"}
        assert len(received) == (2 if with_tool else 1)
        assert received[0]["model"] == "test-model"
        if effort:
            assert received[0]["reasoning_effort"] == effort
        assert "UNRELATED_PRIVATE_CONTEXT_NEVER_LOAD" not in json.dumps(received)
        if with_tool:
            assert received[0]["tools"][0]["function"]["name"] == "host_tool_0"
            assert any(message["role"] == "tool" for message in received[1]["messages"])
            assert any(event["type"] == "tool.result" for event in result.runtime_events)
        else:
            assert not received[0].get("tools")
        assert not list(tmp_path.rglob("*.jsonl"))
    finally:
        server.close()
        await server.wait_closed()


async def test_pi_cancellation_cleans_up_process(monkeypatch, tmp_path):
    from app.application.runtime_config import agent_runtime_config
    from app.runtime.agent_contracts import AgentRunRequest
    from app.runtime.agent_registry import default_agent_registry

    started = asyncio.Event()
    closed = asyncio.Event()

    async def stall(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        started.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()
        closed.set()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "pi")
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(
        settings, "agent_endpoint", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    )
    monkeypatch.setattr(settings, "agent_model", "test-model")
    monkeypatch.setenv(settings.agent_api_key_env, "test-local-key")
    adapter = default_agent_registry().create(agent_runtime_config())
    run_id = str(uuid4())

    async def consume():
        return [event async for event in adapter.events(run_id, AgentRunRequest("hello", tmp_path))]

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 10)
        await adapter.cancel(run_id)
        await asyncio.wait_for(task, 10)
        await asyncio.wait_for(closed.wait(), 5)
        assert not adapter._processes
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
