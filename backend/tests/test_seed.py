from sqlalchemy import func, select

from app.infrastructure.database import Component, SessionLocal
from app.seed import seed


async def test_seed_is_idempotent() -> None:
    first = await seed()
    second = await seed()

    async with SessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(Component))

    assert first == second
    assert count == 2
