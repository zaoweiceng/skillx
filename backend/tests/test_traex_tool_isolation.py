"""Opt-in installed CLI compatibility probe; no credentials or real model calls.

SKILLX_TEST_LOCAL_TRAEX=1 .venv/bin/pytest tests/test_traex_tool_isolation.py -q
"""

import asyncio
import json
import os
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from app.config import get_settings
from app.runtime.agent_contracts import AgentRunRequest
from app.runtime.traex_builtin import TraeXBridgeError, TraeXBuiltinAdapter, _tool_name

pytestmark = pytest.mark.skipif(
    os.environ.get("SKILLX_TEST_LOCAL_TRAEX") != "1" or not shutil.which("traex"),
    reason="Opt-in local installed TraeX compatibility probe",
)


@pytest.mark.parametrize("with_mcp", [False, True])
async def test_installed_cli_exposes_only_host_tools(tmp_path, monkeypatch, with_mcp):
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"local capture complete"}}')

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    real_spawn = asyncio.create_subprocess_exec

    async def local_spawn(*command, **kwargs):
        # Replace rather than inherit auth and provider environment variables.
        kwargs["env"] = {
            "HOME": str(home),
            "TRAE_HOME": str(home),
            "CODEX_HOME": str(home),
            "PATH": os.environ["PATH"],
        }
        overrides = {
            "model_provider": "probe",
            "model": "probe",
            "model_providers.probe.name": "probe",
            "model_providers.probe.base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model_providers.probe.wire_api": "responses",
            "model_providers.probe.requires_openai_auth": False,
            "model_providers.probe.request_max_retries": 0,
            "model_providers.probe.stream_max_retries": 0,
        }
        args = [
            part
            for key, value in overrides.items()
            for part in ("-c", f"{key}={json.dumps(value)}")
        ]
        return await real_spawn(*command[:-1], *args, command[-1], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_spawn)
    descriptor = {
        "capabilityRef": "probe.read",
        "description": "Local probe",
        "outputSchema": {"type": "object", "properties": {}},
        "inputSchema": {"type": "object", "properties": {}},
    }
    adapter = TraeXBuiltinAdapter([shutil.which("traex")])
    try:
        async with asyncio.timeout(20):
            with pytest.raises(TraeXBridgeError, match="local capture complete"):
                async for _ in adapter.events(
                    "local-probe",
                    AgentRunRequest(
                        prompt="Return a JSON object.",
                        working_directory=tmp_path / "run",
                        tool_descriptors=[descriptor] if with_mcp else [],
                    ),
                ):
                    pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert captured, "CLI did not reach the local provider"
    names = [tool.get("name", tool.get("type")) for tool in captured[0].get("tools", [])]
    if with_mcp:
        assert set(names) == {"exec", "wait"}
        # This CLI exposes MCP through code-mode, not direct function declarations.
        prompt = json.dumps(captured[0]["input"])
        assert f"mcp__skillx__{_tool_name('probe.read')}" in prompt
    else:
        assert names == []
