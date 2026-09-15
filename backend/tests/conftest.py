import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

_test_directory = TemporaryDirectory(prefix="skillx-workflow-tests-")
_test_database = Path(_test_directory.name) / "database.sqlite"
os.environ["SKILLX_DATABASE_URL"] = f"sqlite+aiosqlite:///{_test_database}"
os.environ["SKILLX_AGENT_BUILTIN_ENABLED"] = "false"

from app.infrastructure.database import Base, engine  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    async with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(table.delete())
    yield
