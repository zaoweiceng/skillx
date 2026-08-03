from sqlalchemy import func, select

from app.infrastructure.database import Component, SessionLocal
from app.seed import seed


async def test_seed_is_idempotent() -> None:
    first = await seed()
    second = await seed()

    async with SessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(Component))

    assert first == second
    # 六条 Demo 场景中，可作为组件直接启动的有 5 个（E2E-4 走对话候选，不单独 seed 组件）。
    assert count == 5
