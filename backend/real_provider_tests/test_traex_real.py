from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from app.application.agent_provider import probe_provider, reset_provider_probe_cache
from app.config import get_settings


@pytest.mark.skipif(
    os.environ.get("SKILLX_RUN_TRAEX_REAL_CHECK") != "1",
    reason="set SKILLX_RUN_TRAEX_REAL_CHECK=1 to authorize the billable real gate",
)
async def test_actual_traex_probe_round_trip_and_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Actual PATH binary only; fixture executables are not accepted here."""

    executable = shutil.which("traex")
    if executable is None:
        pytest.skip("blocked: traex is not installed on PATH")
    settings = get_settings()
    monkeypatch.setattr(settings, "artifact_root", tmp_path)
    monkeypatch.setattr(settings, "agent_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_builtin_enabled", True)
    monkeypatch.setattr(settings, "agent_command", None)
    monkeypatch.setattr(settings, "agent_provider_type", "unavailable")
    monkeypatch.setattr(settings, "traex_command", None)
    reset_provider_probe_cache()

    result = await probe_provider()
    if result.get("code") == "TRAE_AUTH_REQUIRED":
        pytest.skip("blocked: run `traex logout` then `traex login` in a terminal")
    assert result["ok"] is True, result
    assert result["requestDispatched"] is True
    assert result["usageComplete"] is True
    assert result["toolRoundTrip"] is True
    assert result["protocolCompatible"] is True
    assert result["inputTokens"] <= result["budgetProfile"]["maxInputTokens"]
    assert result["outputTokens"] <= result["budgetProfile"]["maxOutputTokens"]
    assert result["totalTokens"] <= result["budgetProfile"]["maxBillableTokens"]
