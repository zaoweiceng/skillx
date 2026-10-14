"""Malformed transport repair belongs to generation, regardless of artifact kind."""
import pytest
from app.application.agent_host import AgentHost, AgentHostError, AgentHostResult
from app.application.learning_agents import ExtractionPlan, SkillXDraft
from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.tasks import LearningJob
from tests.test_learning_agents import pipeline, data


@pytest.mark.parametrize('role,schema,inputs', [
    ('extractor', ExtractionPlan, {}),
    ('skillx', SkillXDraft, {'availableSkillKinds': ['direct']}),
])
@pytest.mark.parametrize('repaired', [True, False])
async def test_mixed_dsml_submission_gets_one_generation_repair(monkeypatch, role, schema, inputs, repaired):
    worker = await pipeline()
    requests = []
    diagnostic = {'kind': 'invalid_json', 'rawArguments': '{"decision">ready</parameter>\n<parameter: ',
                  'parseError': "Expected ':' after property name at position 11", 'stopReason': 'toolUse', 'truncated': False}
    async def execute(self, request, **kwargs):
        requests.append(request)
        assert not request.include_managed_skills
        assert request.max_steps == 1
        if len(requests) == 1 or not repaired:
            raise AgentHostError('MODEL_MALFORMED_OUTPUT', 'invalid JSON', reported_error={'outputParseError': diagnostic})
        return AgentHostResult(output={'decision': 'skip', 'reason': 'no reusable artifact'})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    if repaired:
        assert (await worker.call(role, schema, inputs)).decision == 'skip'
    else:
        with pytest.raises(AgentHostError):
            await worker.call(role, schema, inputs)
    assert len(requests) == 2
    assert data(requests[1])['validationFeedback']['invalidJsonSubmission'] == diagnostic
    assert (await worker._stage(role))['formatRepairCount'] == 1


@pytest.mark.parametrize('incomplete', [False, True])
async def test_transport_repair_rechecks_durable_usage_before_dispatch(monkeypatch, incomplete):
    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {'budget': {'maxDurationSeconds': 1200, 'maxTokens': 200000, 'startedAt': utcnow().isoformat()}}
    requests = []
    async def execute(self, request, **kwargs):
        requests.append(request)
        # Failed model responses still consume their measured request usage.
        await kwargs['event_sink']({}, {'type': 'agent.step.completed', 'payload': {
            'requestId': 'failed-request', 'inputTokens': 100000, 'outputTokens': 99000, 'usageComplete': not incomplete}})
        raise AgentHostError('MODEL_MALFORMED_OUTPUT', 'bad', reported_error={'outputParseError': {
            'kind': 'invalid_json', 'rawArguments': '{}x', 'stopReason': 'toolUse', 'truncated': False}})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    with pytest.raises(ValueError, match='用量不完整' if incomplete else '预算不足'):
        await worker.call('extractor', ExtractionPlan, {})
    assert len(requests) == 1


@pytest.mark.parametrize('code,diagnostic', [
    ('MODEL_REQUEST_FAILED', None),
    ('MODEL_MALFORMED_OUTPUT', {'kind': 'invalid_json', 'rawArguments': '{}x', 'stopReason': 'length', 'truncated': False}),
    ('MODEL_MALFORMED_OUTPUT', {'kind': 'conflict', 'rawArguments': '{}x', 'stopReason': 'toolUse', 'truncated': False}),
])
async def test_unknown_network_outcome_and_ambiguous_transport_never_retry(monkeypatch, code, diagnostic):
    worker = await pipeline()
    calls = []
    async def execute(*args, **kwargs):
        calls.append(1)
        raise AgentHostError(code, 'rejected', reported_error={'outputParseError': diagnostic})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    with pytest.raises(AgentHostError):
        await worker.call('skillx', SkillXDraft, {'availableSkillKinds': ['direct']})
    assert len(calls) == 1
    assert not (await worker._stage('skillx')).get('formatRepairCount')
