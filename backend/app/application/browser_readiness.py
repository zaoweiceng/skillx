from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.infrastructure.database import (
    BrowserRuntimeHeartbeat,
    SessionLocal,
    rfc3339,
    utcnow,
)

BrowserRuntimeStatus = Literal[
    "available",
    "binary_missing",
    "permission_denied",
    "launch_failed",
    "stale",
    "unconfigured",
]


def _playwright_version() -> str | None:
    try:
        return version("playwright")
    except PackageNotFoundError:
        return None


def classify_launch_error(message: str) -> tuple[BrowserRuntimeStatus, str]:
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in (
            "executable doesn't exist",
            "executable does not exist",
            "browser was not found",
            "no such file or directory",
        )
    ):
        return (
            "binary_missing",
            "Chromium 二进制缺失，请运行 `make browser-install` 后重启 Worker。",
        )
    if any(
        marker in normalized
        for marker in (
            "permission denied",
            "operation not permitted",
            "machport",
            "eacces",
            "eperm",
        )
    ):
        return (
            "permission_denied",
            "Chromium 启动被操作系统权限拒绝，请在具有浏览器启动权限的终端重启 Worker。",
        )
    return (
        "launch_failed",
        "Chromium 启动预检失败，请查看 Worker 日志并修复本机 Playwright/Chromium 环境。",
    )


async def browser_startup_preflight() -> dict[str, Any]:
    settings = get_settings()
    checked_at = utcnow()
    package_version = _playwright_version()
    if not settings.browser_allowed_hosts:
        return {
            "status": "unconfigured",
            "checkedAt": checked_at,
            "chromiumPath": None,
            "chromiumVersion": None,
            "playwrightVersion": package_version,
            "launchSucceeded": False,
            "summary": "未配置 Browser Host allowlist；Browser Runtime 不会执行导航。",
            "errorType": None,
        }

    playwright = None
    browser = None
    chromium_path: str | None = None
    try:
        async with asyncio.timeout(settings.browser_preflight_timeout_seconds):
            playwright = await async_playwright().start()
            chromium_path = playwright.chromium.executable_path
            if not Path(chromium_path).is_file():
                return {
                    "status": "binary_missing",
                    "checkedAt": checked_at,
                    "chromiumPath": chromium_path,
                    "chromiumVersion": None,
                    "playwrightVersion": package_version,
                    "launchSucceeded": False,
                    "summary": (
                        "Chromium 二进制缺失，请运行 `make browser-install` 后重启 Worker。"
                    ),
                    "errorType": "FileNotFoundError",
                }
            browser = await playwright.chromium.launch(headless=True)
            return {
                "status": "available",
                "checkedAt": checked_at,
                "chromiumPath": chromium_path,
                "chromiumVersion": browser.version,
                "playwrightVersion": package_version,
                "launchSucceeded": True,
                "summary": "Worker 已实际启动 Chromium，Browser Runtime 可用。",
                "errorType": None,
            }
    except (TimeoutError, PlaywrightError, OSError) as exc:
        status, summary = classify_launch_error(str(exc))
        return {
            "status": status,
            "checkedAt": checked_at,
            "chromiumPath": chromium_path,
            "chromiumVersion": None,
            "playwrightVersion": package_version,
            "launchSucceeded": False,
            "summary": summary,
            "errorType": type(exc).__name__,
        }
    finally:
        if browser is not None:
            try:
                await browser.close()
            except PlaywrightError:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError:
                pass


async def record_browser_runtime_heartbeat(worker_id: str) -> dict[str, Any]:
    facts = await browser_startup_preflight()
    async with SessionLocal() as session, session.begin():
        heartbeat = await session.get(BrowserRuntimeHeartbeat, worker_id)
        values = {
            "status": facts["status"],
            "checked_at": facts["checkedAt"],
            "chromium_path": facts["chromiumPath"],
            "chromium_version": facts["chromiumVersion"],
            "playwright_version": facts["playwrightVersion"],
            "launch_succeeded": facts["launchSucceeded"],
            "evidence_json": {
                "summary": facts["summary"],
                "errorType": facts["errorType"],
                "allowedHostsConfigured": bool(get_settings().browser_allowed_hosts),
            },
        }
        if heartbeat is None:
            session.add(BrowserRuntimeHeartbeat(worker_id=worker_id, **values))
        else:
            for field, value in values.items():
                setattr(heartbeat, field, value)
    return facts


async def browser_runtime_readiness(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    heartbeat = await session.scalar(
        select(BrowserRuntimeHeartbeat)
        .order_by(BrowserRuntimeHeartbeat.checked_at.desc())
        .limit(1)
    )
    if heartbeat is None:
        status: BrowserRuntimeStatus = (
            "unconfigured" if not settings.browser_allowed_hosts else "stale"
        )
        return {
            "status": status,
            "detail": (
                "未配置 Browser Host allowlist"
                if status == "unconfigured"
                else "尚无 Browser Worker 启动预检事实，请启动独立 Worker。"
            ),
            "workerId": None,
            "checkedAt": None,
            "stale": status == "stale",
            "chromiumExecutable": None,
            "chromiumVersion": None,
            "playwrightVersion": None,
            "launchSucceeded": False,
        }

    checked_at = heartbeat.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    stale = datetime.now(UTC) - checked_at > timedelta(
        seconds=settings.browser_heartbeat_ttl_seconds
    )
    status = "stale" if stale else heartbeat.status
    evidence = heartbeat.evidence_json or {}
    return {
        "status": status,
        "observedStatus": heartbeat.status,
        "detail": (
            "Browser Worker 预检心跳已过期，请检查或重启 Worker。"
            if stale
            else evidence.get("summary", "Browser Worker 已报告启动预检事实。")
        ),
        "workerId": heartbeat.worker_id,
        "checkedAt": rfc3339(heartbeat.checked_at),
        "stale": stale,
        "chromiumExecutable": (
            Path(heartbeat.chromium_path).name if heartbeat.chromium_path else None
        ),
        "chromiumVersion": heartbeat.chromium_version,
        "playwrightVersion": heartbeat.playwright_version,
        "launchSucceeded": heartbeat.launch_succeeded,
        "evidence": evidence,
    }
