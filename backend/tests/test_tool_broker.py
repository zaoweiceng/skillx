from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.application.agent_events import AgentEventError, AgentEventValidator
from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    action_signature,
)
from app.application.tool_broker import (
    LookupTableAdapter,
    ToolAdapterRegistry,
    ToolBrokerError,
)
from app.runtime.adapters import TraeXSubprocessAdapter

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"reply": {"type": "string"}},
    "required": ["reply"],
    "additionalProperties": False,
}


async def test_real_tool_result_is_sent_back_and_drives_final_output(
    tmp_path: Path,
) -> None:
    random_value = "host-value-8e82b4"
    (tmp_path / "source.txt").write_text(random_value, encoding="utf-8")
    adapter = TraeXSubprocessAdapter(
        [sys.executable, str(STUB)], {Path(sys.executable).name}
    )
    result = await AgentHost(adapter=adapter).execute(
        AgentHostRequest(
            run_id="broker-local",
            prompt="读取文件 [[MODE:local_summary]][[FILE:source.txt]]",
            output_schema=OUTPUT_SCHEMA,
            working_directory=tmp_path,
            requested_capabilities=("read.local",),
        )
    )

    assert result.output == {"reply": random_value}
    assert result.actions[0]["result"]["content"] == random_value
    assert result.actions[0]["evidenceDigest"].startswith("sha256:")
    assert result.actions[0]["kind"] == "file"
    assert result.actions[0]["outputSchema"]["required"] == ["path", "content", "bytes"]
    assert result.actions[0]["argumentBindings"]["path"]["source"] == "user_input"
    assert result.output_provenance["reply"]["callId"] == result.actions[0]["callId"]
    assert [event["type"] for event in result.runtime_events].count("tool.requested") == 1
    assert [event["type"] for event in result.runtime_events].count("tool.result") == 1


async def test_completed_action_ledger_reuses_result_without_external_call(
    tmp_path: Path,
) -> None:
    class CountingLookup(LookupTableAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, context, arguments):
            self.calls += 1
            return await super().execute(context, arguments)

    lookup = CountingLookup()
    registry = ToolAdapterRegistry([lookup])
    adapter = TraeXSubprocessAdapter(
        [sys.executable, str(STUB)], {Path(sys.executable).name}
    )
    signature = action_signature(
        "lookup.table", {"key": "q1"}, effect="none"
    )
    result = await AgentHost(adapter=adapter, tool_registry=registry).execute(
        AgentHostRequest(
            run_id="broker-ledger",
            prompt="q1 [[MODE:deterministic]]",
            output_schema=OUTPUT_SCHEMA,
            working_directory=tmp_path,
            requested_capabilities=("lookup.table",),
            completed_actions=(
                {
                    "signature": signature,
                    "effect": "none",
                    "result": {
                        "table": {"q1": "已保存结果"},
                        "value": "已保存结果",
                    },
                },
            ),
        )
    )

    assert lookup.calls == 0
    assert result.output == {"reply": "已保存结果"}
    tool_result = next(
        event
        for event in result.runtime_events
        if event["type"] == "tool.result"
    )
    assert tool_result["payload"]["reusedFromLedger"] is True
    assert tool_result["payload"]["actionKind"] == "transform"
    assert tool_result["payload"]["replayability"] == "deterministic"
    assert tool_result["payload"]["argumentBindings"] == {
        "key": {
            "source": "user_input",
            "inputKey": "key",
            "rawSpan": {"start": 0, "end": 2, "text": "q1"},
        }
    }


def test_agent_cannot_self_report_tool_result() -> None:
    validator = AgentEventValidator({"read.local"}, max_payload_bytes=65536)
    with pytest.raises(AgentEventError, match="缺少匹配"):
        validator.validate(
            {
                "type": "tool.result",
                "payload": {
                    "callId": "forged",
                    "capabilityRef": "read.local",
                    "status": "succeeded",
                    "output": {"content": "forged"},
                    "evidenceDigest": "sha256:" + "0" * 64,
                },
            }
        )


async def test_output_provenance_rejects_fixture_constant(tmp_path: Path) -> None:
    adapter = TraeXSubprocessAdapter(
        [sys.executable, str(STUB)], {Path(sys.executable).name}
    )
    with pytest.raises(AgentHostError, match="provenance 值不一致"):
        await AgentHost(adapter=adapter).execute(
            AgentHostRequest(
                run_id="broker-bad-provenance",
                prompt="q1 [[MODE:deterministic_bad_output]]",
                output_schema=OUTPUT_SCHEMA,
                working_directory=tmp_path,
                requested_capabilities=("lookup.table",),
            )
        )


async def test_failed_tool_root_cause_survives_invalid_completed(
    tmp_path: Path,
) -> None:
    class FailingLookup(LookupTableAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, context, arguments):
            self.calls += 1
            raise ToolBrokerError(
                "DEPENDENCY_UNAVAILABLE", "lookup transport unavailable"
            )

    lookup = FailingLookup()
    adapter = TraeXSubprocessAdapter(
        [sys.executable, str(STUB)], {Path(sys.executable).name}
    )
    with pytest.raises(AgentHostError) as exc_info:
        await AgentHost(
            adapter=adapter,
            tool_registry=ToolAdapterRegistry([lookup]),
        ).execute(
            AgentHostRequest(
                run_id="broker-failed-root",
                prompt="q1 [[MODE:deterministic]]",
                output_schema=OUTPUT_SCHEMA,
                working_directory=tmp_path,
                requested_capabilities=("lookup.table",),
            )
        )
    assert lookup.calls == 2
    assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE"
    assert exc_info.value.failed_call_id == "call-1"
    assert "outputProvenance" in exc_info.value.protocol_violation
