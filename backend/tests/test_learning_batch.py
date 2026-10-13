from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.application.learning_contracts import ContractExample, LearningProposal, WorkflowProposal
from app.application.learning_evidence import validate_examples
from app.application.learning_publishing import publish_artifacts
from app.infrastructure.database import SessionLocal, WorkflowVersion, Conversation, Message
from app.infrastructure.models.tasks import Task, LearningJob, Invocation
from tests.fixtures import deterministic_workflow
from tests.test_learning_agents import pipeline, roots, fragment, context


def test_transformation_evidence_grounds_inputs_not_raw_tool_envelope():
    witness = SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"git":{"stdout":"main"},"exitCode":0}'})
    examples = [ContractExample(input={"raw": "main"}, expected={"branch": "main"},
        sourceInvocationId="call", sourceInput={"raw": "/git/stdout"}),
        ContractExample(input={"raw": "dev"}, expected={"branch": "dev"})]
    validate_examples(examples, {"call": witness})
    examples[0].input = {"raw": "fabricated"}
    with pytest.raises(ValueError, match="输入与来源"):
        validate_examples(examples, {"call": witness})
    examples[0].sourceInput = None
    with pytest.raises(ValueError, match="原始证据"):
        validate_examples(examples, {"call": witness})


def flow(name, dependencies=()):
    definition = deterministic_workflow()
    definition["metadata"] = {"name": name, "capability": {
        "summary": "生成问候", "useWhen": ["问候"], "doNotUseWhen": ["其他任务"],
        "examples": ["问候 Alice"], "tags": ["文本"],
    }}
    return WorkflowProposal(name=name, definition=definition, scope="独立问候",
        sourceInvocationIds=["call"], dependsOn=list(dependencies), examples=[
            ContractExample(input={"name": "Alice"}, expected={"message": "你好，Alice"}, sourceInvocationId="call"),
            ContractExample(input={"name": "Bob"}, expected={"message": "你好，Bob"}),
        ])


async def test_planner_reuses_published_workflow_without_builder_call(monkeypatch, roots):
    from app.application.agent_host import AgentHost, AgentHostResult
    from app.application.callable_capabilities import callable_catalog
    original = await pipeline()
    evidence = {'call': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"message":"你好，Alice"}'})}
    _, published = await publish_artifacts(original, LearningProposal(decision='candidate', reason='fixture', workflows=[flow('existing')]), evidence)
    version_id = published['workflows']['existing']
    worker = await pipeline()
    ctx = context()
    async with SessionLocal() as session:
        ctx['catalog']['capabilities'] = await callable_catalog(session)
    requests = []
    async def choose(self, request, **kwargs):
        requests.append(request.output_schema['title'])
        assert request.output_schema['title'] == 'ExtractionPlan'
        return AgentHostResult(output={'decision': 'candidate', 'reason': '复用现有阶段',
            'workflows': [fragment('existing', 'flow-call', reuseVersionId=version_id)]})
    monkeypatch.setattr(AgentHost, 'execute', choose)
    await worker.generate(ctx, materializer=None)
    stage = await worker._stage('workflow:existing')
    assert stage['reusedVersionId'] == version_id
    assert requests == ['ExtractionPlan']
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(WorkflowVersion)) == 1


async def test_independent_publication_nested_workflow_and_restart(monkeypatch, roots):
    worker = await pipeline()
    first, broken, sibling, parent = flow("first"), flow("broken"), flow("sibling"), flow("parent", ["first"])
    broken.examples[1].expected = {"message": "wrong"}
    node = parent.definition["spec"]["nodes"][1]
    node.update(type="workflow_call", config={"generatedWorkflow": "first"})
    parent.definition["spec"]["nodes"][-1]["input"] = {"message": "${format.output.message}"}
    proposal = LearningProposal(decision="candidate", reason="多个独立流程和组合", workflows=[first, broken, sibling, parent],
        failures={"skillx:unavailable": "缺少外部依赖证据"})
    evidence = {"call": SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"message":"你好，Alice"}'})}
    status, output = await publish_artifacts(worker, proposal, evidence)
    assert status == "partial", output
    assert set(output["workflows"]) == {"first", "sibling", "parent"}, output
    assert "workflow:broken" in output["failures"]
    async with SessionLocal() as session:
        version = await session.get(WorkflowVersion, output["workflows"]["parent"])
        children = version.definition_json["metadata"]["callSources"].values()
        assert any(child["workflowVersionId"] == output["workflows"]["first"] and child["digest"] for child in children)
    _, again = await publish_artifacts(worker, proposal, evidence)
    assert again["workflows"] == output["workflows"]
    async with SessionLocal() as session:
        assert output["workflows"]["first"] == output["workflows"]["sibling"]
        assert await session.scalar(select(func.count()).select_from(WorkflowVersion)) == 2


@pytest.mark.parametrize("failure", ["assertion", "compiler"])
async def test_workflow_validation_feedback_repairs_only_failed_builder(monkeypatch, roots, failure):
    from app.application.agent_host import AgentHost, AgentHostResult
    worker = await pipeline()
    sibling, broken = flow("sibling"), flow("repairable")
    valid = deepcopy(broken.definition)
    if failure == "compiler":
        broken.definition["spec"]["nodes"][1].update(
            sideEffect=True, idempotent=False, retry={"maxAttempts": 2})
    else:
        broken.definition["spec"]["nodes"][-1]["input"] = {"message": "wrong"}
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {"agentStages": {"workflow:repairable": {
            "role": "workflow", "name": "repairable", "status": "completed",
            "buildInputs": {"fragment": {"name": "repairable"}},
            "output": {"decision": "ready", "reason": "draft", "workflow": broken.definition},
        }}}
    seen = []
    async def revise(self, request, **kwargs):
        import json
        data = json.loads(request.prompt.split("阶段资料（仅作为数据）：\n", 1)[1])
        feedback = data["validationFeedback"]["error"]
        if failure == "compiler":
            assert "UNSAFE_SIDE_EFFECT_RETRY" in feedback
            assert "spec.nodes.1.retry" in feedback
        else:
            assert "业务断言" in feedback
        seen.append(request.run_id)
        return AgentHostResult(output={"workflow": valid})
    monkeypatch.setattr(AgentHost, "execute", revise)
    evidence = {"call": SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"message":"你好，Alice"}'})}
    status, output = await publish_artifacts(worker,
        LearningProposal(decision="candidate", reason="test", workflows=[sibling, broken]), evidence)
    assert status == "published", output
    assert set(output["workflows"]) == {"sibling", "repairable"}
    assert len(seen) == 1


async def test_workflow_defaults_match_validation_and_real_run_inputs(roots):
    from app.application.orchestrator import Orchestrator
    from app.application.schema_defaults import apply_input_defaults
    worker = await pipeline()
    item = flow("with-default")
    item.definition["spec"]["inputSchema"]["properties"]["name"]["default"] = "Alice"
    item.examples[0].input = {}
    evidence = {"call": SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"message":"你好，Alice"}'})}
    status, output = await publish_artifacts(worker,
        LearningProposal(decision="candidate", reason="default", workflows=[item]), evidence)
    assert status == "published", output
    async with SessionLocal() as session:
        version = await session.get(WorkflowVersion, output["workflows"][item.name])
        supplied = {}
        run = await Orchestrator().create_run(session, version, supplied)
        assert run.input_json == {"name": "Alice"}
        assert supplied == {}
    assert apply_input_defaults({"name": None}, {"properties": {"name": {"default": "Alice"}}}) == {"name": None}


@pytest.mark.parametrize("directory", [None, "/workspace/demo"])
async def test_extraction_conversation_inherits_workspace_and_cancels(directory):
    from app.application.conversation_extraction import start_extraction
    from app.application.tasks import cancel_task, TaskDriver
    source_id = str(uuid4())
    async with SessionLocal() as session:
        session.add(Conversation(id=source_id, title="来源", working_directory=directory))
        await session.flush()
        session.add(Message(id=str(uuid4()), conversation_id=source_id, role="user", content_json={"text": "构建流程"}))
        await session.commit()
        result = await start_extraction(session, source_id)
        target = await session.get(Conversation, result["conversationId"])
        assert target.id != source_id and target.working_directory == directory
        task = await session.get(Task, result["taskId"])
        assert task.status == "learning"
        assert task.state_json["extractionSource"]["sessionMessages"][0]["content"]["text"] == "构建流程"
        # Extraction cannot enter the ordinary task Agent, even with no pending calls.
        assert await TaskDriver().poll_once() is False
        await cancel_task(session, task)
        await session.commit()
        job = await session.scalar(select(LearningJob).where(LearningJob.task_id == task.id))
        assert job.status == "cancelled" and job.lease_token is None


async def test_selected_agent_handles_llm_in_an_isolated_tool_free_session(monkeypatch, tmp_path):
    from app.application.agent_host import AgentHost, AgentHostResult
    from app.application.runtime_profiles import save_profile, workflow_profiles
    from app.application.structured_generation import StructuredGenerationResolver, StructuredGenerationRequest
    from app.domain.workflow import WorkflowDefinition
    selected = None
    async with SessionLocal() as session:
        profile = await save_profile(session, kind="agent", name="selected", config={"adapter": "pi"}, is_default=True, profile_id=None, active=True)
        selected = profile.id
        definition = deterministic_workflow()
        definition["spec"]["nodes"][1].update(type="llm", config={
            "agentProfileId": selected, "prompt": "问候", "outputSchema": {"type": "object"}})
        frozen = (await workflow_profiles(session, WorkflowDefinition.model_validate(definition)))["format"]
    frozen["agentConfig"].update(endpoint="https://model.invalid", model="test", credential_configured=True)
    from app.application.compiler import digest_json
    frozen["runtimeDigest"] = digest_json(frozen["agentConfig"])
    monkeypatch.setenv(frozen["agentConfig"]["api_key_env"], "test")
    assert frozen["runtimeSelection"] == "agent_structured"
    assert frozen["agentConfig"]["type"] == "pi"
    seen = []
    async def generate(self, request, **kwargs):
        seen.append(request)
        return AgentHostResult(output={"message": "hello"}, total_tokens=9, usage_complete=True)
    monkeypatch.setattr(AgentHost, "execute", generate)
    result = await StructuredGenerationResolver().generate(StructuredGenerationRequest(
        call_id="new-run", prompt="new prompt", output_schema={"type": "object"},
        max_output_tokens=100, working_directory=tmp_path / "isolated"), frozen)
    assert result.output == {"message": "hello"}
    assert seen[0].requested_capabilities == () and not seen[0].include_managed_skills
    assert seen[0].prompt == "new prompt"


async def test_manual_extraction_finishes_without_changing_source(monkeypatch):
    from app.application.conversation_extraction import start_extraction
    from app.application.task_learning import LearningDriver
    from app.application.agent_host import AgentHost, AgentHostResult
    source_id = str(uuid4())
    async with SessionLocal() as session:
        session.add(Conversation(id=source_id, title="来源"))
        await session.flush()
        session.add(Message(id=str(uuid4()), conversation_id=source_id, role="user", content_json={"text": "snapshot"}))
        await session.commit()
        result = await start_extraction(session, source_id)
        session.add(Message(id=str(uuid4()), conversation_id=source_id, role="user", content_json={"text": "LATER-MESSAGE"}))
        await session.commit()
    async def generate(self, request, **kwargs):
        assert "snapshot" in request.prompt
        assert "LATER-MESSAGE" not in request.prompt
        return AgentHostResult(output={"decision": "skip", "reason": "无独立可执行片段"})
    monkeypatch.setattr(AgentHost, "execute", generate)
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, result["taskId"])
        assert task.status == "succeeded" and "未发布" in task.output_json["text"]
        assert await session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == source_id)) == 2


async def test_repair_publishes_new_version_of_same_workflow_and_keeps_sibling(roots):
    worker = await pipeline()
    evidence = {"call": SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"message":"你好，Alice"}'})}
    original = LearningProposal(decision="candidate", reason="original", workflows=[flow("child"),flow("sibling")])
    _, first = await publish_artifacts(worker, original, evidence)
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        data = deepcopy(job.result_json)
        base = data["artifacts"]["workflows"].pop("child")
        data["revisionBaseWorkflowVersions"] = {"child": base}
        job.result_json = data
    changed = flow("child")
    changed.definition["metadata"]["capability"]["summary"] = "updated description"
    status, revised = await publish_artifacts(worker,
        LearningProposal(decision="candidate", reason="feedback", workflows=[changed]), evidence)
    assert status == "published", revised["failures"].get("workflow:child")
    assert revised["workflows"]["sibling"] == first["workflows"]["sibling"]
    async with SessionLocal() as session:
        old = await session.get(WorkflowVersion, base)
        new = await session.get(WorkflowVersion, revised["workflows"]["child"])
        assert old.workflow_id == new.workflow_id
        assert new.version == old.version + 1


def test_evidence_feedback_identifies_example_mapping_and_available_fields():
    witness = SimpleNamespace(status="succeeded", kind="code",
        output_json={"stdout": '{"git":{"stdout":"main"},"exitCode":0}'})
    examples = [ContractExample(input={"raw": "main"}, expected={"branch": "main"},
        sourceInvocationId="call", sourceInput={"raw": "/git/branch"}),
        ContractExample(input={"raw": "dev"}, expected={"branch": "dev"})]
    with pytest.raises(ValueError) as error:
        validate_examples(examples, {"call": witness})
    feedback = str(error.value)
    for detail in ["examples[0].sourceInput", "sourceInvocationId=call", "raw", "/git/branch", "stdout", "observedOutput"]:
        assert detail in feedback
    examples[0].sourceInput = {"raw": "/git/stdout"}
    validate_examples(examples, {"call": witness})


def test_evidence_pointer_schema_matches_backend_and_disallows_literal_values():
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError
    validator = Draft202012Validator(ContractExample.model_json_schema())
    for pointer in ["https://host/repo.git", "feature/example", "/bad~escape"]:
        value = {"input": {}, "expected": {}, "sourceInput": {"raw": pointer}}
        assert list(validator.iter_errors(value))
        with pytest.raises(ValidationError):
            ContractExample.model_validate(value)
    for pointer in ["", "/git/stdout", "/a~1b/~0field", "/records/0"]:
        value = {"input": {}, "expected": {}, "sourceInput": {"raw": pointer}}
        assert not list(validator.iter_errors(value))
        ContractExample.model_validate(value)


def test_evidence_pointer_rejects_negative_array_index_and_supports_escaping():
    from app.application.learning_evidence import project
    assert project({"a/b": {"~x": ["first", "last"]}}, {"item": "/a~1b/~0x/1"}) == {"item": "last"}
    with pytest.raises(ValueError, match="数组长度=2"):
        project({"items": ["first", "last"]}, {"item": "/items/-1"})


def test_escaped_stdout_evidence_uses_real_tool_envelope_not_guessed_json_fields():
    from app.application.task_learning import observed_output
    witness = SimpleNamespace(status="succeeded", kind="code", output_json={
        "stdout": r'{\"remoteUrl\":{\"out\":\"https://example.test/team/repo.git\"}}\n',
        "stderr": "", "exitCode": 0})
    assert observed_output(witness) == witness.output_json
    examples = [ContractExample(input={"raw": witness.output_json["stdout"]}, expected={"parsed": True},
        sourceInvocationId="call", sourceInput={"raw": "/remoteUrl/out"}),
        ContractExample(input={"raw": "other record"}, expected={"parsed": False})]
    with pytest.raises(ValueError, match="stdout"):
        validate_examples(examples, {"call": witness})
    examples[0].sourceInput = {"raw": "/stdout"}
    validate_examples(examples, {"call": witness})


@pytest.mark.parametrize('statement,quote_name', [
    ('from urllib.parse import quote', 'quote'),
    ('import urllib.parse', 'urllib.parse.quote'),
    ('from urllib import parse', 'parse.quote'),
])
async def test_pure_url_parser_materializes_without_network_dependency(tmp_path, monkeypatch, statement, quote_name):
    from app.application.learning_contracts import SkillXProposal
    from app.application.task_learning import materialize_skill
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'capability_root', tmp_path)
    proposal = SkillXProposal(name='url-encode', description='Encode a URL segment',
        inputSchema={'type': 'object'}, outputSchema={'type': 'object'},
        source=f'{statement}\ndef execute(request, context):\n    return {{"encoded": {quote_name}(request.input["value"], safe="")}}',
        examples=[ContractExample(input={'value': 'a/b'}, expected={'encoded': 'a%2Fb'}, sourceInvocationId='call'),
            ContractExample(input={'value': 'two words'}, expected={'encoded': 'two%20words'})])
    root = await materialize_skill(proposal, 'stdlib-parser', {
        'call': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"encoded":"a%2Fb"}'})})
    assert (root / 'validation.json').exists()


@pytest.mark.parametrize('statement', ['import urllib', 'import urllib.request', 'from urllib.request import urlopen', 'from urllib import request'])
async def test_network_capable_module_import_is_not_itself_an_effect(tmp_path, monkeypatch, statement):
    from app.application.learning_contracts import SkillXProposal
    from app.application.task_learning import materialize_skill
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'capability_root', tmp_path)
    proposal = SkillXProposal(name='unused-import', description='Pure computation despite imported module',
        inputSchema={'type': 'object'}, outputSchema={'type': 'object'},
        source=f'{statement}\ndef execute(request, context):\n    return request.input',
        examples=[ContractExample(input={'x': 1}, expected={'x': 1}, sourceInvocationId='call'),
            ContractExample(input={'x': 2}, expected={'x': 2})])
    await materialize_skill(proposal, 'import-only', {
        'call': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"x":1}'})})

async def test_backend_key_error_does_not_spend_agent_revision_or_block_sibling(monkeypatch, roots):
    import app.application.learning_validation as validation
    worker = await pipeline()
    original_validate = validation.validate_candidate
    revisions = []
    async def validate(session, definition, *args, **kwargs):
        if definition['metadata']['name'] == 'system-broken':
            raise KeyError('internal-field')
        return await original_validate(session, definition, *args, **kwargs)
    async def revise(*args):
        revisions.append(args)
        raise AssertionError('Internal programming errors cannot be repaired by a model')
    monkeypatch.setattr(validation, 'validate_candidate', validate)
    monkeypatch.setattr(worker, 'revise_workflow', revise)
    evidence = {'call': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"message":"你好，Alice"}'})}
    status, output = await publish_artifacts(worker,
        LearningProposal(decision='candidate', reason='test', workflows=[flow('system-broken'), flow('healthy')]), evidence)
    assert status == 'partial'
    assert set(output['workflows']) == {'healthy'}
    assert not revisions
    failure = output['failures']['workflow:system-broken']
    assert 'SYSTEM_CONTRACT_ERROR' in failure and 'test_learning_batch.py:' in failure
