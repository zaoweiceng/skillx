from uuid import uuid4

from sqlalchemy import func, select

from app.application.compiler import WorkflowCompiler
from app.infrastructure.database import (
    Component,
    Run,
    SessionLocal,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowVersion,
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


async def test_seed_upgrades_pre_025_weather_without_losing_history() -> None:
    await seed()
    async with SessionLocal() as session, session.begin():
        component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        assert component is not None
        version = await session.get(WorkflowVersion, component.default_version_id)
        workflow = await session.get(Workflow, component.workflow_id)
        assert version is not None and workflow is not None
        # 构造一个与当前受管理 seed 存在通用差异（节点显示名）的旧版本，触发原位升级；
        # 不依赖任何领域字面量（如天气 Presentation 模板）。
        old_definition = {
            **version.definition_json,
            "spec": {
                **version.definition_json["spec"],
                "nodes": [
                    {**node, "displayName": "查询天气（历史版本）"}
                    if node["id"] == "weather"
                    else node
                    for node in version.definition_json["spec"]["nodes"]
                ],
            },
        }
        old_digest = WorkflowCompiler().compile(old_definition).digest
        version.definition_json = old_definition
        version.digest = old_digest
        version.presentation_policy_json = {}
        version.revision_lineage_json = {}
        component.seed_key = None
        component.managed_revision = None
        workflow.draft_json = old_definition
        workflow.draft_revision = 1
        workflow.draft_metadata_json = {}
        history = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            component_id=component.id,
            status="succeeded",
            input_json={"city": "上海"},
            output_json={"city": "上海", "condition": "多云", "temperature": 19},
        )
        session.add(history)
        old_version_id = version.id
        history_id = history.id

    await seed()
    await seed()

    async with SessionLocal() as session:
        component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        assert component is not None
        versions = (
            await session.scalars(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == component.workflow_id)
                .order_by(WorkflowVersion.version)
            )
        ).all()
        upgraded = await session.get(WorkflowVersion, component.default_version_id)
        history = await session.get(Run, history_id)
    assert len(versions) == 2
    assert versions[0].id == old_version_id
    assert history is not None and history.workflow_version_id == old_version_id
    assert component.seed_key == "local-weather"
    assert component.managed_revision == 2
    assert upgraded is not None
    # 通用平台 seed 不再内置任何领域专用 Presentation：升级后的版本无天气模板等字面量。
    assert upgraded.presentation_policy_json == {}
    assert "presentationPolicy" not in upgraded.definition_json.get("metadata", {})


async def test_seed_does_not_overwrite_user_weather_draft() -> None:
    await seed()
    async with SessionLocal() as session, session.begin():
        component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        assert component is not None
        version = await session.get(WorkflowVersion, component.default_version_id)
        workflow = await session.get(Workflow, component.workflow_id)
        assert version is not None and workflow is not None
        component.seed_key = None
        component.managed_revision = None
        version.revision_lineage_json = {}
        workflow.draft_revision = 2
        workflow.draft_json = {
            **version.definition_json,
            "metadata": {
                **version.definition_json["metadata"],
                "name": "user-weather-draft",
            },
        }
        workflow.draft_metadata_json = {}
        version_count = await session.scalar(
            select(func.count())
            .select_from(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow.id)
        )

    await seed()

    async with SessionLocal() as session:
        component = await session.scalar(
            select(Component).where(Component.name == "本地天气查询")
        )
        workflow = await session.get(Workflow, component.workflow_id)
        current_count = await session.scalar(
            select(func.count())
            .select_from(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow.id)
        )
    assert component.seed_key is None
    assert current_count == version_count
    assert workflow.draft_json["metadata"]["name"] == "user-weather-draft"
