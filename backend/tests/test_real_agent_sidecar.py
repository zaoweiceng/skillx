from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.application.agent_host import AgentHost, AgentHostRequest
from app.config import get_settings

SIDECAR = Path(__file__).resolve().parents[2] / "examples" / "openai_agent_sidecar.py"


@pytest.fixture
def model_server() -> tuple[str, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "real-call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup__table",
                                "arguments": json.dumps({"key": "q1"}),
                            },
                        }
                    ],
                }
            else:
                assert requests[-1]["messages"][-1]["role"] == "tool"
                message = {
                    "role": "assistant",
                    "content": json.dumps(
                        {"reply": "第一季度：营收增长 12%"},
                        ensure_ascii=False,
                    ),
                }
            body = json.dumps(
                {
                    "model": "real-test-model",
                    "choices": [{"message": message}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/chat/completions", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_real_openai_sidecar_round_trips_host_tool_result(
    model_server: tuple[str, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    endpoint, requests = model_server
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_provider_type", "real")
    monkeypatch.setattr(settings, "model_endpoint", endpoint)
    monkeypatch.setattr(settings, "model_name", "real-test-model")
    monkeypatch.setattr(settings, "model_api_key_env", "SKILLX_TEST_MODEL_KEY")
    monkeypatch.setenv("SKILLX_TEST_MODEL_KEY", "test-only")
    monkeypatch.setattr(
        settings, "traex_command", [sys.executable, str(SIDECAR)]
    )
    monkeypatch.setattr(
        settings, "traex_allowed_executables", [Path(sys.executable).name]
    )

    result = await AgentHost().execute(
        AgentHostRequest(
            run_id="real-sidecar",
            prompt="查询 q1",
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            },
            working_directory=tmp_path,
            requested_capabilities=("lookup.table",),
        )
    )

    assert result.output == {"reply": "第一季度：营收增长 12%"}
    assert [item["type"] for item in result.runtime_events].count("tool.requested") == 1
    assert [item["type"] for item in result.runtime_events].count("tool.result") == 1
    assert len(requests) == 2
