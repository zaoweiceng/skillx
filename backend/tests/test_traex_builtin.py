from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.runtime_config import agent_runtime_config
from app.config import get_settings
from app.runtime.traex_builtin import TraeXBuiltinAdapter


def _fake_traex(tmp_path: Path, *, fail_auth: bool = False) -> Path:
    executable = tmp_path / "traex"
    body = f"""#!{sys.executable}
import json
import sys
import urllib.request

args = sys.argv[1:]
print(json.dumps({{"type": "thread.started", "thread_id": "test"}}), flush=True)
print(json.dumps({{"type": "turn.started"}}), flush=True)
if {fail_auth!r}:
    print("401 unauthorized: access token expired", file=sys.stderr, flush=True)
    raise SystemExit(1)
output_path = args[args.index("--output-last-message") + 1]
config = next(args[index + 1] for index, item in enumerate(args) if item == "-c" and
              args[index + 1].startswith("mcp_servers.skillx.url="))
url = config.split("=", 1)[1].strip('"')
def rpc(payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={{"content-type": "application/json", "accept": "application/json"}},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status == 202:
            return {{}}
        return json.loads(response.read())
rpc({{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {{
    "protocolVersion": "2025-03-26", "capabilities": {{}},
    "clientInfo": {{"name": "test", "version": "1"}}
}}}})
rpc({{"jsonrpc": "2.0", "method": "notifications/initialized"}})
tools = rpc({{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}})["result"]["tools"]
called = rpc({{
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {{"name": tools[0]["name"], "arguments": {{"key": "q1"}}}}
}})
value = called["result"]["structuredContent"]["value"]
with open(output_path, "w", encoding="utf-8") as output:
    json.dump({{"reply": value}}, output, ensure_ascii=False)
print(json.dumps({{
    "type": "turn.completed",
    "model": "fake-traex-model",
    "modelProviderId": "traex-test",
    "usage": {{"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
}}), flush=True)
"""
    executable.write_text(body, encoding="utf-8")
    executable.chmod(0o755)
    return executable


async def test_builtin_bridge_round_trips_host_tool_result(tmp_path: Path) -> None:
    executable = _fake_traex(tmp_path)
    result = await AgentHost(
        adapter=TraeXBuiltinAdapter([str(executable)])
    ).execute(
        AgentHostRequest(
            run_id="builtin-round-trip",
            prompt="查询 q1",
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
                "additionalProperties": False,
            },
            working_directory=tmp_path / "run",
            requested_capabilities=("lookup.table",),
        )
    )

    assert result.output == {"reply": "第一季度：营收增长 12%"}
    assert result.request_dispatched is True
    assert result.total_tokens == 15
    assert result.usage_complete is True
    assert result.provider == "traex-test"
    assert result.model == "fake-traex-model"
    assert [item["type"] for item in result.runtime_events].count("tool.requested") == 1
    assert [item["type"] for item in result.runtime_events].count("tool.result") == 1


async def test_builtin_bridge_maps_auth_failure(tmp_path: Path) -> None:
    executable = _fake_traex(tmp_path, fail_auth=True)
    with pytest.raises(AgentHostError) as error:
        await AgentHost(
            adapter=TraeXBuiltinAdapter([str(executable)])
        ).execute(
            AgentHostRequest(
                run_id="builtin-auth",
                prompt="probe",
                output_schema={"type": "object"},
                working_directory=tmp_path / "run",
            )
        )

    assert error.value.code == "TRAE_AUTH_REQUIRED"
    assert error.value.request_dispatched is True


def test_runtime_auto_discovers_builtin_traex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_builtin_enabled", True)
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(settings, "agent_provider_type", "unavailable")
    monkeypatch.setattr(settings, "traex_command", None)
    monkeypatch.setattr(
        "app.application.runtime_config.shutil.which",
        lambda command: "/usr/local/bin/traex" if command == "traex" else None,
    )

    runtime = agent_runtime_config()

    assert runtime.type == "traex_builtin"
    assert runtime.source == "auto_discovered"
    assert runtime.provider == "traex"
    assert runtime.protocol == "traex-cli-jsonl+mcp"
    assert runtime.adapter_digest.startswith("sha256:")
    assert "SKILLX_AGENT_API_KEY" not in os.environ


async def test_missing_builtin_traex_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_builtin_enabled", True)
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(settings, "agent_provider_type", "unavailable")
    monkeypatch.setattr(settings, "traex_command", None)
    monkeypatch.setattr(
        "app.application.runtime_config.shutil.which",
        lambda _command: None,
    )

    with pytest.raises(AgentHostError) as error:
        await AgentHost().execute(
            AgentHostRequest(
                run_id="missing-traex",
                prompt="test",
                output_schema={"type": "object"},
                working_directory=tmp_path,
            )
        )

    assert error.value.code == "TRAE_NOT_INSTALLED"
