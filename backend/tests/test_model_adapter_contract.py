from dataclasses import asdict
from pathlib import Path

import pytest
from app.application.agent_host import AgentHost, AgentHostError, AgentHostResult
from app.application.runtime_config import agent_runtime_config
from app.application.structured_generation import (
    StructuredGenerationResolver, StructuredGenerationRequest, agent_structured_snapshot,
)
from app.runtime.models import ModelRuntimeError


@pytest.mark.parametrize("code", ["MODEL_AUTH_FAILED", "RATE_LIMITED", "AGENT_TIMEOUT"])
async def test_structured_agent_errors_preserve_dispatch_without_switching(monkeypatch, code):
    snapshot = asdict(agent_runtime_config())
    snapshot.update(type="fixture", provider="fixture", command=["fixture"])
    calls = []
    async def fail(self, request, **kwargs):
        calls.append(request)
        raise AgentHostError(code, "failure", request_dispatched=True)
    monkeypatch.setattr(AgentHost, "execute", fail)
    with pytest.raises(ModelRuntimeError) as error:
        await StructuredGenerationResolver().generate(StructuredGenerationRequest(
            "isolated", "prompt", {"type": "object"}, 100, Path("/tmp/test-agent")),
            agent_structured_snapshot(snapshot))
    assert error.value.code == code and error.value.request_dispatched
    assert len(calls) == 1 and calls[0].requested_capabilities == ()
