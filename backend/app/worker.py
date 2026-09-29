from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from app.application.agent_run import AgentRunWorker
from app.application.browser_readiness import record_browser_runtime_heartbeat
from app.application.orchestrator import WorkerService, reconcile_recent_runs
from app.application.product_history import (
    PresentationDriver,
    finalize_ready_turns,
)
from app.application.recovery import RecoveryDriver
from app.application.task_recovery import TaskCheckpointDriver
from app.config import get_settings
from app.infrastructure.database import SessionLocal, init_database

logger = logging.getLogger("skillx.worker")


async def browser_health_loop(worker_id: str, stopping: asyncio.Event) -> None:
    """Run Browser startup probes outside the serial work-claim loop."""

    settings = get_settings()
    while not stopping.is_set():
        try:
            facts = await record_browser_runtime_heartbeat(worker_id)
            logger.info(
                "browser runtime preflight worker=%s status=%s",
                worker_id,
                facts["status"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Browser health is isolated from work claims.
            logger.exception("browser runtime preflight failed")
        try:
            await asyncio.wait_for(
                stopping.wait(),
                timeout=settings.browser_preflight_interval_seconds,
            )
        except TimeoutError:
            pass


async def run_worker(
    worker_id: str | None = None,
    *,
    task_recovery_enabled: bool = True,
) -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        await init_database()
    service = WorkerService(worker_id or settings.worker_id, settings.lease_seconds)
    # Agent 长任务与工作流节点共用同一 Worker 进程：先持久化后执行的 ConversationRun
    # 由 AgentRunWorker 领取租约执行，API 重启不影响已领取/待领取任务。
    agent_worker = AgentRunWorker(f"{service.worker_id}-agent")
    recovery_driver = RecoveryDriver()
    task_recovery_driver = TaskCheckpointDriver(f"{service.worker_id}-task-recovery")
    presentation_driver = PresentationDriver()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stopping.set)

    # 启动时先做一次 Artifact reconcile：补落位“DB 已提交、promote 未完成”的孤儿窗口。
    try:
        async with SessionLocal() as session, session.begin():
            report = await reconcile_recent_runs(session)
        logger.info("worker startup reconcile: %s", report)
    except Exception:  # noqa: BLE001 - reconcile 失败不应阻止 Worker 启动。
        logger.exception("worker startup reconcile failed")

    browser_health_task = asyncio.create_task(
        browser_health_loop(service.worker_id, stopping),
        name=f"browser-health-{service.worker_id}",
    )

    # 明确的轮询就绪信号：黑盒 harness 据此确认 Worker 已进入领取循环，
    # 而不是仅检查进程尚未退出。
    print(f"worker polling worker_id={service.worker_id}", flush=True)
    from app.application.invocations import InvocationDriver
    from app.application.task_learning import LearningDriver
    from app.application.tasks import TaskDriver

    async def driver_loop(driver, method="poll_once"):
        while not stopping.is_set():
            try:
                processed = await getattr(driver, method)()
            except Exception:
                logger.exception("durable driver failed: %s", type(driver).__name__)
                processed = False
            if not processed:
                await asyncio.sleep(settings.worker_poll_seconds)

    durable_drivers = [asyncio.create_task(driver_loop(TaskDriver())),
                       asyncio.create_task(driver_loop(LearningDriver()))]
    durable_drivers.extend(asyncio.create_task(driver_loop(InvocationDriver())) for _ in range(4))
    durable_drivers.extend(asyncio.create_task(driver_loop(
        WorkerService(f"{service.worker_id}-node-{index}", settings.lease_seconds), "run_once"
    )) for index in range(3))
    durable_drivers.extend(asyncio.create_task(driver_loop(driver))
                           for driver in [agent_worker, recovery_driver, presentation_driver])
    if task_recovery_enabled:
        durable_drivers.append(asyncio.create_task(driver_loop(task_recovery_driver)))
    ticks = 0
    try:
        while not stopping.is_set():
            processed = await service.run_once()
            try:
                async with SessionLocal() as session, session.begin():
                    await finalize_ready_turns(session)
            except Exception:
                logger.exception("turn finalization failed")
            # 周期性 reconcile（约每 60 次空闲轮询一次），限制批量，形成可观察 GC。
            ticks += 1
            if ticks % 60 == 0:
                try:
                    async with SessionLocal() as session, session.begin():
                        await reconcile_recent_runs(session, limit=20)
                except Exception:  # noqa: BLE001
                    logger.exception("periodic reconcile failed")
            if not processed:
                try:
                    await asyncio.wait_for(
                        stopping.wait(), timeout=settings.worker_poll_seconds
                    )
                except TimeoutError:
                    pass
    finally:
        stopping.set()
        for driver_task in durable_drivers:
            driver_task.cancel()
        await asyncio.gather(*durable_drivers, return_exceptions=True)
        browser_health_task.cancel()
        try:
            await browser_health_task
        except asyncio.CancelledError:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="SkillX workflow worker")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--disable-task-recovery", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run_worker(
            args.worker_id,
            task_recovery_enabled=not args.disable_task_recovery,
        )
    )


if __name__ == "__main__":
    main()
