from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from app.application.agent_run import AgentRunWorker
from app.application.orchestrator import WorkerService, reconcile_recent_runs
from app.application.recovery import RecoveryDriver
from app.application.task_recovery import TaskCheckpointDriver
from app.config import get_settings
from app.infrastructure.database import SessionLocal, init_database

logger = logging.getLogger("skillx.worker")


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

    # 明确的轮询就绪信号：黑盒 harness 据此确认 Worker 已进入领取循环，
    # 而不是仅检查进程尚未退出。
    print(f"worker polling worker_id={service.worker_id}", flush=True)
    ticks = 0
    while not stopping.is_set():
        processed = await service.run_once()
        try:
            processed_agent = await agent_worker.poll_once()
        except Exception:  # noqa: BLE001 - 记录根因后继续工作流循环，不静默吞掉。
            logger.exception("agent worker poll_once failed on worker %s", service.worker_id)
            processed_agent = False
        try:
            processed_recovery = await recovery_driver.poll_once()
        except Exception:  # noqa: BLE001 - 恢复失败记录根因后继续。
            logger.exception("recovery driver poll_once failed on worker %s", service.worker_id)
            processed_recovery = False
        processed_task_recovery = False
        if task_recovery_enabled:
            try:
                processed_task_recovery = await task_recovery_driver.poll_once()
            except Exception:  # noqa: BLE001 - 保留 checkpoint 供租约过期后重新领取。
                logger.exception(
                    "task recovery driver poll_once failed on worker %s",
                    service.worker_id,
                )
        # 周期性 reconcile（约每 60 次空闲轮询一次），限制批量，形成可观察 GC。
        ticks += 1
        if ticks % 60 == 0:
            try:
                async with SessionLocal() as session, session.begin():
                    await reconcile_recent_runs(session, limit=20)
            except Exception:  # noqa: BLE001
                logger.exception("periodic reconcile failed")
        if (
            not processed
            and not processed_agent
            and not processed_recovery
            and not processed_task_recovery
        ):
            try:
                await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
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
