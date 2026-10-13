"""Route observed dependency defects back to their own Agent, never edit packages."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from app.infrastructure.database import Capability, CapabilityVersion, SessionLocal
from app.infrastructure.models.tasks import Invocation, LearningJob


async def repair_targets(observations):
    targets = []
    async with SessionLocal() as session:
        for observation in observations:
            version_id = observation.get('capabilityVersionId')
            if not version_id:
                continue
            version = await session.get(CapabilityVersion, version_id)
            if not version:
                continue
            capability = await session.get(Capability, version.capability_id)
            targets.append({**observation, 'name': capability.name,
                'digest': version.digest,
                'contract': version.manifest_json['interfaces'][observation['interface']]})
    return targets


def check_requests(requests, targets):
    allowed = {(target['nodeId'], target['capabilityVersionId']) for target in targets}
    selected = set()
    for request in requests:
        key = (request.nodeId, request.versionId)
        if key not in allowed:
            raise ValueError('依赖修订必须指向本次实际执行观察中的节点和版本')
        if request.versionId in selected:
            raise ValueError('同一依赖版本只能请求一次定向修订')
        selected.add(request.versionId)


async def repair_dependencies(pipeline, requests, targets):
    from app.application.learning_agents import ReusableFragment, SkillXDraft, check_evidence_ids
    from app.application.learning_contracts import LearningProposal
    from app.application.learning_publishing import publish_artifacts
    from app.application.task_learning import materialize_skill, observed_output
    check_requests(requests, targets)
    replacements = []
    for request in requests:
        target = next(t for t in targets if t['nodeId'] == request.nodeId and t['capabilityVersionId'] == request.versionId)
        name, key = target['name'], 'skillx:' + target['name']
        async with SessionLocal() as session:
            current = await pipeline._owned_job(session)
            state = deepcopy(current.result_json)
            stage = state.get('agentStages', {}).get(key)
            if not stage or not (stage.get('output') or {}).get('skillx'):
                origin = await session.scalar(select(LearningJob).where(
                    LearningJob.result_json['artifacts']['skillx'][name].as_string() == request.versionId
                ).order_by(LearningJob.created_at.desc()))
                original = (origin.result_json.get('agentStages', {}).get(key) if origin else None)
                if not original:
                    raise ValueError('依赖缺少原生成 Agent 的可验证证据与构建记录，不能自动覆盖')
                stage = deepcopy(original)
                if not stage.get('buildInputs'):
                    # Legacy jobs saved SkillX outputs but only workflow buildInputs.
                    # Reconstruct evidence scope from their recorded extractor plan,
                    # never infer it from code, package names or the current task.
                    plan = origin.result_json.get('agentStages', {}).get('extractor', {}).get('output', {})
                    assigned = next((item for item in plan.get('skillx', []) if item.get('name') == name), None)
                    if not assigned or not assigned.get('sourceInvocationIds'):
                        raise ValueError('依赖原始提炼计划未记录证据范围，不能自动覆盖')
                    calls = list(await session.scalars(select(Invocation).where(
                        Invocation.id.in_(assigned['sourceInvocationIds']))))
                    stage['buildInputs'] = {'fragment': assigned, 'calls': [
                        {'id': call.id, 'kind': call.kind, 'status': call.status,
                         'input': call.request_json, 'output': call.output_json,
                         'observedOutput': observed_output(call)} for call in calls]}

                stage['revisionCount'] = 0  # Fresh job, not a reset of this job's spent revision.
            if stage.get('revisionCount', 0) >= 1:
                raise ValueError('依赖已使用本轮定向修订预算，保留其他独立产物')
            capability_version = await session.get(CapabilityVersion, request.versionId)
            capability = await session.get(Capability, capability_version.capability_id)
            if capability.current_version_id != request.versionId:
                raise ValueError('依赖当前版本已变化，禁止覆盖其他修订')
            fragment = ReusableFragment.model_validate(stage['buildInputs']['fragment'])
            evidence = {row.id: row for row in await session.scalars(select(Invocation).where(
                Invocation.id.in_(fragment.sourceInvocationIds)))}
            if not evidence or any(id not in evidence or evidence[id].status != 'succeeded' for id in fragment.sourceInvocationIds):
                raise ValueError('依赖原始成功证据不可用，不能执行自动修订')
            previous = SkillXDraft.model_validate(stage['output']).skillx
            stage.update(status='needs_revision', revisionCount=1,
                error={'error': request.feedback, 'actualObservation': target,
                       'requiredAction': '修订实现并添加覆盖此缺陷的独立可重复样例；不能弱化工作流断言。保持已发布接口兼容。'})
            state.setdefault('agentStages', {})[key] = stage
            state.setdefault('revisionBaseVersions', {})[name] = request.versionId
            state.setdefault('dependencyRepairs', {})[request.versionId] = {'name': name, 'status': 'running', 'nodeId': request.nodeId}
            current.result_json = state
            await session.commit()

        def validate(draft):
            if not draft.skillx or draft.skillx.name != name:
                raise ValueError('依赖修订不能替换能力身份')
            value = draft.skillx
            if value.inputSchema != previous.inputSchema or value.outputSchema != previous.outputSchema or value.kind != previous.kind or value.executionProfile != previous.executionProfile or value.dependencies != previous.dependencies:
                raise ValueError('依赖实现修订必须保持已发布输入输出接口兼容')
            source_ids = [example.sourceInvocationId for example in value.examples if example.sourceInvocationId]
            if source_ids or value.executionProfile != 'local-command':
                check_evidence_ids(source_ids, fragment)
        draft = await pipeline.repair_skillx(fragment, stage, validate)
        root = await pipeline.validate_skillx(name, lambda: materialize_skill(
            draft.skillx, pipeline.job_id + '-dependency-' + str(uuid4()), evidence))
        pipeline.materialized_roots[name] = root
        # Only after new implementation and examples pass do we replace this job's
        # binding. Immutable old package/version and unrelated artifacts remain.
        async with SessionLocal() as session:
            current = await pipeline._owned_job(session)
            state = deepcopy(current.result_json)
            artifacts = state.setdefault('artifacts', {})
            old = artifacts.setdefault('skillx', {}).pop(name, None)
            current.result_json = state
            await session.commit()
        _, result = await publish_artifacts(pipeline, LearningProposal(
            decision='candidate', reason='实际工作流验证反馈驱动依赖 Agent 修订', skillx=[draft.skillx]), evidence)
        version_id = result['artifacts']['skillx'].get(name)
        if not version_id:
            if old:
                async with SessionLocal() as session:
                    current = await pipeline._owned_job(session)
                    state = deepcopy(current.result_json)
                    state['artifacts']['skillx'][name] = old
                    current.result_json = state
                    await session.commit()
            raise ValueError('依赖修订发布失败：' + str(result['failures']))
        async with SessionLocal() as session:
            version = await session.get(CapabilityVersion, version_id)
            current = await pipeline._owned_job(session)
            state = deepcopy(current.result_json)
            state['dependencyRepairs'][request.versionId].update(status='published', versionId=version_id)
            current.result_json = state
            await session.commit()
            replacements.append({'name': name, 'id': version_id, 'previousVersionId': request.versionId,
                'digest': version.digest, 'interfaces': version.manifest_json['interfaces']})
    return replacements
