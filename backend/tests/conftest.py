import os
from collections.abc import AsyncIterator

import pytest_asyncio

os.environ["SKILLX_DATABASE_URL"] = "sqlite+aiosqlite:////tmp/skillx-workflow-tests.db"

from app.infrastructure.database import Base, engine  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
