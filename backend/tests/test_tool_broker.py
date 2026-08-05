from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.application.agent_events import AgentEventError, AgentEventValidator
from app.application.agent_host import AgentHost, AgentHostRequest
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
    assert [event["type"] for event in result.runtime_events].count("tool.requested") == 1
    assert [event["type"] for event in result.runtime_events].count("tool.result") == 1


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
