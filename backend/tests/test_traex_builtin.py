from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.runtime_config import agent_runtime_config
from app.config import get_settings
from app.runtime.traex_builtin import (
    TraeXBridgeError,
    TraeXBuiltinAdapter,
    _tool_descriptor_map,
    _tool_name,
)


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


async def test_builtin_bridge_round_trips_host_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
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


async def test_builtin_bridge_maps_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
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
    # 认证在任何模型 dispatch 之前失败：不得伪造 dispatched，attempted 由上层单独记录。
    assert error.value.request_dispatched is False


async def test_builtin_bridge_rejects_untrusted_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台隔离根目录之外的 cwd（含 symlink 逃逸）必须 fail closed，不启动 TraeX。"""

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(get_settings(), "artifact_root", artifact_root)
    executable = _fake_traex(tmp_path)
    with pytest.raises(AgentHostError) as error:
        await AgentHost(
            adapter=TraeXBuiltinAdapter([str(executable)])
        ).execute(
            AgentHostRequest(
                run_id="builtin-untrusted",
                prompt="probe",
                output_schema={"type": "object"},
                working_directory=outside,
            )
        )

    assert error.value.code == "TRAE_WORKDIR_UNTRUSTED"


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


def _descriptor(capability_ref: str) -> dict[str, object]:
    return {
        "capabilityRef": capability_ref,
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
    }


def test_tool_name_is_one_to_one_for_punctuation_variants() -> None:
    # a.b 与 a_b 归一化后不再碰撞，各自映射到唯一名称。
    assert _tool_name("a.b") != _tool_name("a_b")
    descriptors = _tool_descriptor_map([_descriptor("a.b"), _descriptor("a_b")])
    assert len(descriptors) == 2
    refs = {value["capabilityRef"] for value in descriptors.values()}
    assert refs == {"a.b", "a_b"}


def test_tool_name_handles_unicode_and_long_refs() -> None:
    long_ref = "workflow." + "x" * 200 + ".slice"
    unicode_ref = "查询.天气"
    descriptors = _tool_descriptor_map(
        [
            _descriptor(long_ref),
            _descriptor(unicode_ref),
            _descriptor("workflow.search"),
        ]
    )
    assert len(descriptors) == 3
    for name in descriptors:
        # MCP/TraeX 安全名称：仅字母数字、下划线与连字符，且长度受限。
        assert re.fullmatch(r"[A-Za-z0-9_-]+", name)
        assert len(name) <= 62
    # 原始 capabilityRef 始终作为业务身份保留，不被规范化名称替换。
    assert {value["capabilityRef"] for value in descriptors.values()} == {
        long_ref,
        unicode_ref,
        "workflow.search",
    }


def test_tool_descriptor_map_fails_closed_on_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 强制两个不同 ref 归一化为同名，验证反向映射守卫 fail closed，绝不静默覆盖。
    monkeypatch.setattr(
        "app.runtime.traex_builtin._tool_name", lambda _ref: "collision__deadbeef"
    )
    with pytest.raises(TraeXBridgeError) as error:
        _tool_descriptor_map([_descriptor("cap.one"), _descriptor("cap.two")])

    assert error.value.code == "AGENT_PROTOCOL_INCOMPATIBLE"
