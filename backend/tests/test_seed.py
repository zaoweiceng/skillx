from sqlalchemy import func, select

from app.infrastructure.database import (
    Component,
    SessionLocal,
    WorkflowCapabilityManifest,
)
from app.seed import seed


async def test_seed_is_idempotent() -> None:
    first = await seed()
    second = await seed()

    async with SessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(Component))

    assert first == second
    # 六条 Demo 场景中，可作为组件直接启动的有 5 个（E2E-4 走对话候选，不单独 seed 组件）。
    assert count == 5


async def test_seed_weather_manifest_has_semantic_catalog_metadata() -> None:
    await seed()

    async with SessionLocal() as session:
        component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        assert component is not None
        manifest = await session.scalar(
            select(WorkflowCapabilityManifest).where(
                WorkflowCapabilityManifest.workflow_version_id
                == component.default_version_id
            )
        )

    assert manifest is not None
    assert manifest.manifest_json["summary"]
    assert manifest.manifest_json["useWhen"]
    assert manifest.manifest_json["doNotUseWhen"]
    assert manifest.manifest_json["examples"]
