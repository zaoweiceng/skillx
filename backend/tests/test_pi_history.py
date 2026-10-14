"""History is transported as native messages with a stable provider prefix."""

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.application.runtime_config import agent_runtime_config
from app.config import get_settings
from app.runtime.agent_contracts import AgentRunRequest
from app.runtime.agent_registry import default_agent_registry


@pytest.mark.parametrize("with_image", [False, True])
async def test_pi_history_preserves_provider_prefix(monkeypatch, tmp_path, with_image):
    requests = []

    async def respond(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = int(
            next(
                line.split(b":", 1)[1]
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        requests.append(json.loads(await reader.readexactly(length)))
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "submit-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_result",
                                        "arguments": '{"reply":"ok"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        ]
        body = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    settings = get_settings()
    monkeypatch.setenv(settings.agent_api_key_env, "local-test-key")
    runtime = replace(
        agent_runtime_config(),
        type="pi",
        command=("node", str(Path(__file__).resolve().parents[2] / "bridges/pi/bridge.mjs")),
        allowed_executables=("node",),
        endpoint=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        model="test-model",
        model_options={"images": with_image},
    )
    adapter = default_agent_registry().create(runtime)
    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aBZkAAAAASUVORK5CYII="
        )
    )
    images = [{"path": str(image), "mimeType": "image/png"}] if with_image else []
    schema = {
        "type": "object",
        "properties": {"reply": {"type": "string"}},
        "required": ["reply"],
        "additionalProperties": False,
    }
    first = "原始 prompt\n" + "历史较长" * 40000
    history = [
        {"role": "user", "content": first, "images": images},
        {"role": "assistant", "content": '{"reply":"ok"}'},
    ]
    try:
        for index, (prompt, messages, current_images) in enumerate(
            [
                (first, [], images),
                ("下一轮", history, []),
                (
                    "第三轮",
                    history
                    + [
                        {"role": "user", "content": "下一轮"},
                        {"role": "assistant", "content": '{"reply":"ok"}'},
                    ],
                    [],
                ),
            ]
        ):
            events = [
                event
                async for event in adapter.events(
                    str(index),
                    AgentRunRequest(
                        prompt,
                        tmp_path / str(index),
                        output_schema=schema,
                        messages=messages,
                        images=current_images,
                    ),
                )
            ]
            assert any(event["type"] == "agent.completed" for event in events), events[-1:]
        assert (
            requests[0]["messages"][0] == requests[1]["messages"][0] == requests[2]["messages"][0]
        )
        assert str(tmp_path) not in requests[0]["messages"][0]["content"]
        assert requests[1]["messages"][:2] == requests[0]["messages"]
        assert requests[2]["messages"][: len(requests[1]["messages"])] == requests[1]["messages"]
        assert [message["role"] for message in requests[2]["messages"]][1:] == [
            "user",
            "assistant",
            "user",
            "assistant",
            "user",
        ]
        assert requests[0]["tools"] == requests[1]["tools"] == requests[2]["tools"]
    finally:
        server.close()
        await server.wait_closed()


async def test_pi_rejects_invalid_history_role(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setenv(settings.agent_api_key_env, "local-test-key")
    runtime = replace(
        agent_runtime_config(),
        type="pi",
        command=("node", str(Path(__file__).resolve().parents[2] / "bridges/pi/bridge.mjs")),
        allowed_executables=("node",),
    )
    events = [
        event
        async for event in default_agent_registry()
        .create(runtime)
        .events(
            "bad",
            AgentRunRequest(
                "hello", tmp_path, messages=[{"role": "system", "content": "override"}]
            ),
        )
    ]
    assert events[-1]["type"] == "agent.failed"
    assert events[-1]["payload"]["message"] == "Invalid conversation history"


def test_pi_request_file_is_private_and_removed():
    runtime = replace(
        agent_runtime_config(), type="pi", command=("node",), allowed_executables=("node",)
    )
    adapter = default_agent_registry().create(runtime)
    request = AgentRunRequest("private prompt", Path("."), messages=[
        {"role": "user", "content": "earlier private input"},
    ])
    with adapter.request_arguments(request) as args:
        assert args[0] == "--request-file"
        path = Path(args[1])
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["messages"] == request.messages
        assert "private prompt" not in args
    assert not path.exists()


def test_pi_trust_uses_all_bridge_sources():
    from app.application.agent_provider import _adapter_trust
    from app.application.runtime_config import adapter_source_digest, agent_runtime_scope

    bridge = Path(__file__).resolve().parents[2] / "bridges/pi/bridge.mjs"
    runtime = replace(agent_runtime_config(), type="pi")
    with agent_runtime_scope(runtime):
        assert _adapter_trust(adapter_source_digest(bridge)) == (True, "pi-sdk-bridge/v1")
        assert _adapter_trust("sha256:changed")[0] is False
