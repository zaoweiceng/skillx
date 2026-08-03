from __future__ import annotations

import argparse
import asyncio
import signal

from app.application.agent_run import AgentRunWorker
from app.application.orchestrator import WorkerService
from app.config import get_settings
from app.infrastructure.database import init_database


async def run_worker(worker_id: str | None = None) -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        await init_database()
    service = WorkerService(worker_id or settings.worker_id, settings.lease_seconds)
    # Agent 长任务与工作流节点共用同一 Worker 进程：先持久化后执行的 ConversationRun
    # 由 AgentRunWorker 领取租约执行，API 重启不影响已领取/待领取任务。
    agent_worker = AgentRunWorker(f"{service.worker_id}-agent")
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stopping.set)
    # 明确的轮询就绪信号：黑盒 harness 据此确认 Worker 已进入领取循环，
    # 而不是仅检查进程尚未退出。
    print(f"worker polling worker_id={service.worker_id}", flush=True)
    while not stopping.is_set():
        processed = await service.run_once()
        try:
            processed_agent = await agent_worker.poll_once()
        except Exception:  # noqa: BLE001 - Agent 领取失败不应终止工作流循环。
            processed_agent = False
        if not processed and not processed_agent:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="SkillX workflow worker")
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args()
    asyncio.run(run_worker(args.worker_id))


if __name__ == "__main__":
    main()
