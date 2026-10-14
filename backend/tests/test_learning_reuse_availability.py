import pytest

from app.application.learning_context import require_available_reuse


@pytest.mark.parametrize('group', ['skills', 'skillx', 'workflows'])
def test_learning_reuse_rejects_identity_absent_from_live_catalog(group):
    context = {'existingSkills': [{'id': 'available-guide'}],
               'catalog': {'capabilities': [{'id': 'available-version'}]}}
    with pytest.raises(ValueError, match='当前可用目录'):
        require_available_reuse(context, group, 'disabled-or-deleted-version')
    require_available_reuse(context, group,
                            'available-guide' if group == 'skills' else 'available-version')


async def test_learning_catalog_excludes_disabled_workflow_and_reuse_rejects_it():
    from app.api.tasks import CapabilityAvailabilityUpdate, update_capability_availability
    from app.application.tasks import task_catalog
    from app.infrastructure.database import SessionLocal
    from tests.test_workflow_catalog_plans import _published

    _, version, _ = await _published()
    async with SessionLocal() as session:
        context = {'catalog': await task_catalog(session)}
        require_available_reuse(context, 'workflows', version.id)
        await update_capability_availability(version.workflow_id,
                                            CapabilityAvailabilityUpdate(enabled=False), session)
        context['catalog'] = await task_catalog(session)
        with pytest.raises(ValueError, match='当前可用目录'):
            require_available_reuse(context, 'workflows', version.id)


def test_workflow_requirement_rejects_skillx_only_and_allows_single_workflow():
    from app.application.learning_agents import ExtractionPlan, validate_extraction
    from tests.test_learning_agents import context, fragment
    ctx = context()
    ctx['requirements'] = {'requireWorkflow': True}
    plan = ExtractionPlan(decision='candidate', reason='fragment', skillx=[fragment('compute', 'script-call')])
    with pytest.raises(ValueError, match='不能仅生成'):
        validate_extraction(plan, ctx)
    plan = ExtractionPlan(decision='candidate', reason='workflow', workflow=fragment('one-flow', 'flow-call'))
    validate_extraction(plan, ctx)
