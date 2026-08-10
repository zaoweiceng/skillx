from datetime import timedelta

from app.application.browser_readiness import (
    browser_runtime_readiness,
    browser_startup_preflight,
    classify_launch_error,
)
from app.config import get_settings
from app.infrastructure.database import (
    BrowserRuntimeHeartbeat,
    SessionLocal,
    utcnow,
)


def test_browser_launch_error_classification_is_actionable() -> None:
    assert classify_launch_error("Executable doesn't exist at /tmp/chromium")[0] == (
        "binary_missing"
    )
    status, summary = classify_launch_error(
        "MachPort rendezvous failed: Permission denied (1100)"
    )
    assert status == "permission_denied"
    assert "权限" in summary
    assert classify_launch_error("unexpected browser exit")[0] == "launch_failed"


async def test_browser_preflight_reports_unconfigured_without_launching(
    monkeypatch,
) -> None:
    monkeypatch.setattr(get_settings(), "browser_allowed_hosts", [])

    facts = await browser_startup_preflight()

    assert facts["status"] == "unconfigured"
    assert facts["launchSucceeded"] is False
    assert facts["chromiumPath"] is None


async def test_browser_readiness_uses_persisted_worker_fact_and_expires(
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "browser_allowed_hosts", ["127.0.0.1"])
    monkeypatch.setattr(settings, "browser_heartbeat_ttl_seconds", 120)
    checked_at = utcnow()
    async with SessionLocal() as session, session.begin():
        session.add(
            BrowserRuntimeHeartbeat(
                worker_id="browser-worker",
                status="permission_denied",
                checked_at=checked_at,
                chromium_path="/tmp/chromium",
                playwright_version="1.0",
                launch_succeeded=False,
                evidence_json={"summary": "操作系统权限拒绝"},
            )
        )

    async with SessionLocal() as session:
        fresh = await browser_runtime_readiness(session)
    assert fresh["status"] == "permission_denied"
    assert fresh["workerId"] == "browser-worker"
    assert fresh["launchSucceeded"] is False

    async with SessionLocal() as session, session.begin():
        row = await session.get(BrowserRuntimeHeartbeat, "browser-worker")
        assert row is not None
        row.checked_at = checked_at - timedelta(seconds=121)
    async with SessionLocal() as session:
        stale = await browser_runtime_readiness(session)
    assert stale["status"] == "stale"
    assert stale["observedStatus"] == "permission_denied"
