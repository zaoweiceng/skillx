from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.agent_provider import probe_provider, reset_provider_probe_cache
from app.config import get_settings

SIDECAR = Path(__file__).resolve().parents[2] / "examples" / "openai_agent_sidecar.py"
STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


@pytest.fixture
def model_server() -> tuple[str, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append(payload)
            if len(requests) == 1:
                prompt = str(requests[-1]["messages"][-1]["content"])
                challenge = re.search(r"key=(probe-[0-9a-f]+)", prompt)
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "real-call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup__table",
                                "arguments": json.dumps(
                                    {"key": challenge.group(1) if challenge else "q1"}
                                ),
                            },
                        }
                    ],
                }
            else:
                assert requests[-1]["messages"][-1]["role"] == "tool"
                tool_output = json.loads(requests[-1]["messages"][-1]["content"])
                message = {
                    "role": "assistant",
                    "content": json.dumps(
                        {"reply": tool_output["value"]},
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


async def test_provider_probe_requires_explicit_dispatch_usage_and_random_challenge(
    model_server: tuple[str, list[dict]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    endpoint, _requests = model_server
    settings = get_settings()
    reset_provider_probe_cache()
    monkeypatch.setattr(settings, "artifact_root", tmp_path)
    monkeypatch.setattr(settings, "agent_provider_type", "real")
    monkeypatch.setattr(settings, "model_endpoint", endpoint)
    monkeypatch.setattr(settings, "model_name", "real-test-model")
    monkeypatch.setattr(settings, "model_api_key_env", "SKILLX_TEST_MODEL_KEY")
    monkeypatch.setenv("SKILLX_TEST_MODEL_KEY", "test-only")
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(SIDECAR)])
    monkeypatch.setattr(
        settings, "traex_allowed_executables", [Path(sys.executable).name]
    )

    result = await probe_provider()

    assert result["ok"] is True
    assert result["requestDispatched"] is True
    assert result["usageComplete"] is True
    assert result["totalTokens"] == 30
    assert result["adapterTrusted"] is True
    assert result["adapterVersion"] == "openai-agent-sidecar/v1"
    assert result["challenge"].startswith("probe-")


async def test_provider_probe_does_not_infer_dispatch_from_agent_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    reset_provider_probe_cache()
    digest = "sha256:" + hashlib.sha256(STUB.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, "artifact_root", tmp_path)
    monkeypatch.setattr(settings, "agent_runtime_type", "traex_managed")
    monkeypatch.setattr(settings, "agent_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(
        settings, "agent_allowed_executables", [Path(sys.executable).name]
    )
    monkeypatch.setattr(settings, "agent_trusted_adapter_digests", [digest])

    result = await probe_provider()

    assert result["ok"] is False
    assert result["requestDispatched"] is False


@pytest.mark.parametrize(
    ("status", "body", "expected_code"),
    [
        (401, '{"error":{"message":"invalid key"}}', "MODEL_AUTH_FAILED"),
        (429, '{"error":{"message":"rate limit"}}', "RATE_LIMITED"),
        (
            400,
            '{"error":{"message":"response_format json_schema unsupported"}}',
            "MODEL_CAPABILITY_UNSUPPORTED",
        ),
        (503, '{"error":{"message":"unavailable"}}', "DEPENDENCY_UNAVAILABLE"),
    ],
)
async def test_real_sidecar_reports_provider_compatibility_diagnostics(
    status: int,
    body: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            encoded = body.encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_provider_type", "real")
    monkeypatch.setattr(
        settings,
        "model_endpoint",
        f"http://127.0.0.1:{server.server_port}/chat/completions",
    )
    monkeypatch.setattr(settings, "model_name", "real-test-model")
    monkeypatch.setattr(settings, "model_api_key_env", "SKILLX_TEST_MODEL_KEY")
    monkeypatch.setenv("SKILLX_TEST_MODEL_KEY", "test-only")
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(SIDECAR)])
    monkeypatch.setattr(
        settings,
        "traex_allowed_executables",
        [Path(sys.executable).name],
    )
    try:
        with pytest.raises(AgentHostError) as error:
            await AgentHost().execute(
                AgentHostRequest(
                    run_id=f"provider-{status}",
                    prompt="兼容性测试",
                    output_schema={
                        "type": "object",
                        "properties": {"reply": {"type": "string"}},
                        "required": ["reply"],
                        "additionalProperties": False,
                    },
                    working_directory=tmp_path,
                )
            )
        assert error.value.code == expected_code
    finally:
        server.shutdown()
        thread.join(timeout=5)
