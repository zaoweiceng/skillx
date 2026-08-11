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


async def test_all_seed_manifests_have_semantic_catalog_metadata() -> None:
    await seed()

    async with SessionLocal() as session:
        components = (await session.scalars(select(Component))).all()
        manifests = (
            await session.scalars(select(WorkflowCapabilityManifest))
        ).all()

    assert len(manifests) == len(components)
    for manifest in manifests:
        assert manifest.manifest_json["summary"]
        assert manifest.manifest_json["useWhen"]
        assert manifest.manifest_json["doNotUseWhen"]
        assert manifest.manifest_json["examples"]
        assert manifest.manifest_json["tags"]
        assert manifest.manifest_json["inputSynopsis"]
        assert manifest.manifest_json["outputSynopsis"]
