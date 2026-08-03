from __future__ import annotations

import argparse
import asyncio
import signal

from app.application.orchestrator import WorkerService
from app.config import get_settings
from app.infrastructure.database import init_database


async def run_worker(worker_id: str | None = None) -> None:
    settings = get_settings()
    if settings.auto_create_schema:
        await init_database()
    service = WorkerService(worker_id or settings.worker_id, settings.lease_seconds)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stopping.set)
    while not stopping.is_set():
        processed = await service.run_once()
        if not processed:
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
