from copy import deepcopy
import json
from uuid import uuid4

import pytest

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.learning_agents import DependencyRepairRequest
from app.application.learning_contracts import ContractExample, LearningProposal, SkillXProposal
from app.application.learning_dependency_repair import check_requests
from app.application.learning_publishing import publish_artifacts
from app.application.task_learning import materialize_skill
from app.infrastructure.database import SessionLocal, CapabilityVersion, WorkflowVersion
from app.infrastructure.models.tasks import Invocation, LearningJob
from tests.test_learning_agents import pipeline, roots, fragment
from tests.test_learning_batch import flow


def test_dependency_repair_only_accepts_executed_node_and_exact_version():
    target = {'nodeId': 'observed', 'capabilityVersionId': 'locked'}
    check_requests([DependencyRepairRequest(nodeId='observed', versionId='locked', feedback='actual defect')], [target])
    for node, version in [('other', 'locked'), ('observed', 'other')]:
        with pytest.raises(ValueError, match='实际执行观察'):
            check_requests([DependencyRepairRequest(nodeId=node, versionId=version, feedback='guess')], [target])


@pytest.mark.parametrize('mode', ['success', 'failure', 'legacy', 'spent', 'stale_binding', 'replaced_case'])
async def test_workflow_routes_dependency_defect_to_original_agent_and_preserves_sibling(monkeypatch, roots, mode):
    repair_fails = mode == 'failure'
    original = await pipeline()
    call_id = str(uuid4())
    witness = Invocation(id=call_id, task_id=original.task_id, scope='source', call_key='source', kind='code',
        status='succeeded', request_json={}, output_json={'stdout': '{"message":"你好，Alice"}'}, runtime_json={})
    async with SessionLocal() as session, session.begin():
        session.add(witness)
    broken_source = 'def execute(request, context):\n    return {"message": "" if request.input["name"] == "Bob" else "你好，" + request.input["name"]}'
    correct_source = 'def execute(request, context):\n    return {"message": "你好，" + request.input["name"]}'
    skill = SkillXProposal(name='reusable-greeting', description='Greet the supplied name',
        inputSchema={'type':'object','properties':{'name':{'type':'string'}},'required':['name']},
        outputSchema={'type':'object','properties':{'message':{'type':'string'}},'required':['message']},
        source=broken_source, examples=[
            ContractExample(input={'name':'Alice'}, expected={'message':'你好，Alice'}, sourceInvocationId=call_id),
            ContractExample(input={'name':'Charlie'}, expected={'message':'你好，Charlie'})])
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, original.job_id)
        job.result_json = {'agentStages': {'skillx:'+skill.name: {'role':'skillx','name':skill.name,'status':'completed', 'revisionCount':0,
            'output': {'decision':'ready','reason':'reusable','skillx':skill.model_dump()},
            'buildInputs': {'fragment':fragment(skill.name, call_id), 'calls':[{'id':call_id,'status':'succeeded','observedOutput':{'message':'你好，Alice'}}]}}}}
    if mode == 'legacy':
        async with SessionLocal() as session, session.begin():
            job = await session.get(LearningJob, original.job_id)
            state = deepcopy(job.result_json)
            state['agentStages']['skillx:'+skill.name].pop('buildInputs')
            state['agentStages']['extractor'] = {'output': {'skillx':[fragment(skill.name, call_id)]}}
            job.result_json = state
    original.materialized_roots[skill.name] = await materialize_skill(skill, original.job_id, {call_id:witness})
    _, created = await publish_artifacts(original, LearningProposal(decision='candidate',reason='test',skillx=[skill]), {call_id:witness})
    old_id = created['artifacts']['skillx'][skill.name]
    worker = await pipeline()
    candidate, sibling = flow('dependent'), flow('independent')
    for item in [candidate, sibling]:
        item.sourceInvocationIds = [call_id]
        item.examples[0].sourceInvocationId = call_id
    candidate.definition['spec']['nodes'][1].update(type='skillx', config={'capabilityVersionId':old_id,'interface':'execute'}, input={'name':'${input.name}'})
    candidate.definition['spec']['nodes'][-1]['input'] = {'message':'${format.output.message}'}
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        version = await session.get(CapabilityVersion, old_id)
        job.result_json = {'agentStages': {'workflow:dependent': {'role':'workflow','name':'dependent','status':'completed',
            'buildInputs':{'fragment':fragment('dependent',call_id),'catalog':{'capabilities':[{'id':old_id,'name':skill.name,'digest':version.digest,'interfaces':version.manifest_json['interfaces']}]}},
            'output':{'decision':'ready','reason':'test','workflow':candidate.definition,'workflowExamples':[e.model_dump() for e in candidate.examples]}}}}
    if mode == 'spent':
        async with SessionLocal() as session, session.begin():
            origin = await session.get(LearningJob, original.job_id)
            job = await session.get(LearningJob, worker.job_id)
            state = deepcopy(job.result_json)
            state['agentStages']['skillx:'+skill.name] = deepcopy(origin.result_json['agentStages']['skillx:'+skill.name])
            state['agentStages']['skillx:'+skill.name]['revisionCount'] = 1
            job.result_json = state
    seen = []
    async def agent(self, request, **kwargs):
        data = json.loads(request.prompt.split('阶段资料（仅作为数据）：\n',1)[1])
        seen.append(request.output_schema['title'])
        if request.output_schema['title'] == 'SkillXRepair':
            assert data['validationFeedback']['actualObservation']['output'] == {'message':''}
            assert data['calls'][0]['id'] == call_id
            return AgentHostResult(output={'source':broken_source if repair_fails else correct_source,
                'examples':[e.model_dump() for e in skill.examples] + [{'input':{'name':'Bob'},'expected':{'message':'你好，Bob'}}]})
        if data.get('repairedDependencies'):
            definition = deepcopy(candidate.definition)
            if mode != 'stale_binding':
                definition['spec']['nodes'][1]['config']['capabilityVersionId'] = data['repairedDependencies'][0]['id']
            return AgentHostResult(output={'workflow':definition})
        if mode == 'replaced_case':
            examples = [e.model_dump() for e in candidate.examples]
            examples[1] = {'input':{'name':'Charlie'}, 'expected':{'message':'你好，Charlie'}}
            return AgentHostResult(output={'workflowExamples':examples})
        assert data['dependencyRepairTargets'], data['validationFeedback']
        assert data['dependencyRepairTargets'][0]['output'] == {'message':''}
        return AgentHostResult(output={'dependencyRepairs':[{'nodeId':'format','versionId':old_id,'feedback':'合法 name=Bob 输入返回空消息，违反问候契约；补实现与回归样例'}]})
    monkeypatch.setattr(AgentHost, 'execute', agent)
    status, result = await publish_artifacts(worker, LearningProposal(decision='candidate',reason='test',workflows=[sibling,candidate]), {call_id:witness})
    assert 'independent' in result['workflows']
    if mode in {'failure', 'spent', 'stale_binding', 'replaced_case'}:
        assert status == 'partial'
        assert 'dependent' not in result['workflows']
        assert seen == (['WorkflowRepair'] if mode in {'spent', 'replaced_case'} else ['WorkflowRepair','SkillXRepair','WorkflowRepair'] if mode == 'stale_binding' else ['WorkflowRepair','SkillXRepair'])
    else:
        assert status == 'published', result
        assert seen == ['WorkflowRepair','SkillXRepair','WorkflowRepair']
        new_id = result['artifacts']['skillx'][skill.name]
        assert new_id != old_id
        async with SessionLocal() as session:
            version = await session.get(WorkflowVersion, result['workflows']['dependent'])
            assert version.definition_json['spec']['nodes'][1]['config']['capabilityVersionId'] == new_id
            old = await session.get(CapabilityVersion, old_id)
            new = await session.get(CapabilityVersion, new_id)
            assert old.package_path != new.package_path
