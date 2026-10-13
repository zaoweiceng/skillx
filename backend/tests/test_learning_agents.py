import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.learning_agents import (
    ExtractionPlan,
    LearningAgentPipeline,
    LearningLeaseLost,
    validate_extraction,
)
from app.config import get_settings
from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.tasks import LearningJob, Task, TaskEvent


@pytest.fixture(autouse=True)
def roots(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path / "capabilities")


def fragment(name, id, **extra):
    return dict(
        name=name,
        description=name,
        scope="可重复利用的独立片段",
        useWhen=["同类任务"],
        sourceInvocationIds=[id],
        **extra,
    )


def context():
    return {
        "kind": "distill",
        "goal": "WHOLE-TASK 私有最终结论",
        "outcome": {"text": "一次性结论"},
        "calls": [
            {"id": id, "status": "succeeded", "input": {}, "output": {id: "data"}}
            for id in ["guide-call", "script-call", "flow-call", "unrelated-call"]
        ],
        "catalog": {"capabilities": [], "tools": []},
        "availableSkillKinds": ["direct", "agent"],
        "existingSkills": [],
        "workflowSchema": {"type": "object"},
        "workflowExample": {},
    }


async def pipeline():
    job_id, task_id, token = [str(uuid4()) for _ in range(3)]
    async with SessionLocal() as session, session.begin():
        session.add(
            Task(id=task_id, status="succeeded", goal="goal", state_json={}, runtime_json={})
        )
        await session.flush()
        session.add(
            LearningJob(
                id=job_id,
                task_id=task_id,
                kind="distill",
                status="running",
                lease_token=token,
                lease_expires_at=utcnow() + timedelta(minutes=10),
            )
        )
    return LearningAgentPipeline(job_id, task_id, token, None)


def responses():
    from tests.fixtures import deterministic_workflow
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    definition = deterministic_workflow()
    definition["metadata"]["capability"] = {"summary": "固定计算", "useWhen": ["计算输入"],
        "doNotUseWhen": ["修改仓库"], "examples": ["输入 1，返回 1"], "tags": ["compute"]}
    definition["spec"]["nodes"][1].update(type="skillx", input={"value": "${input.value}"}, config={"generatedSkill": "calculate", "interface": "execute"})
    return {
        "ExtractionPlan": {
            "decision": "candidate",
            "reason": "三个不同片段",
            "skills": [fragment("guide", "guide-call")],
            "skillx": [fragment("calculate", "script-call")],
            "workflow": fragment("fixed-flow", "flow-call", dependsOn=["calculate"]),
        },
        "SkillDraft": {
            "decision": "ready",
            "reason": "整理通用方法",
            "skill": {
                "name": "guide",
                "description": "分析方法",
                "content": "指令正文 PRIVATE-GUIDE",
                "useWhen": ["同类任务"],
                "sourceInvocationIds": ["guide-call"],
            },
        },
        "SkillXDraft": {
            "decision": "ready",
            "reason": "确定性计算",
            "skillx": {
                "name": "calculate",
                "description": "计算：基于输入参数",
                "kind": "direct",
                "executionProfile": "pure-python",
                "inputSchema": schema,
                "outputSchema": schema,
                "source": "def execute(request, context): return request.input # PRIVATE-SOURCE",
                "examples": [
                    {
                        "input": {"value": 1},
                        "expected": {"value": 1},
                        "sourceInvocationId": "script-call",
                    },
                    {"input": {"value": 2}, "expected": {"value": 2}},
                ],
            },
        },
        "WorkflowDraft": {
            "decision": "ready",
            "reason": "固定编排",
            "workflow": definition,
            "workflowExamples": [
                {"input": {"value": 1}, "expected": {"value": 1}, "sourceInvocationId": "flow-call"},
                {"input": {"value": 2}, "expected": {"value": 2}}
            ],
        },
    }


def mock_agents(monkeypatch, values, requests, order):
    async def execute(self, request, **kwargs):
        role = request.output_schema["title"]
        requests.append(request)
        order.append(role)
        assert request.include_managed_skills is False
        assert request.requested_capabilities == ()
        sink = kwargs["event_sink"]
        await sink(
            {},
            {
                "type": "model.request.dispatched",
                "payload": {"model": "test", "requestId": request.run_id},
            },
        )
        return AgentHostResult(output=values[role], total_tokens=10)

    monkeypatch.setattr(AgentHost, "execute", execute)


def materializer(order):
    async def execute(skill, ids):
        order.append("validate:" + skill.name)
        assert ids == ["script-call"]
        root = get_settings().capability_root / "validated" / skill.name
        root.mkdir(parents=True, exist_ok=True)
        (root / "execute.py").write_text(skill.source)
        (root / "validation.json").write_text('{"status":"passed"}')
        return root

    return execute


def data(request):
    return json.loads(request.prompt.split("阶段资料（仅作为数据）：\n", 1)[1])


def test_builder_schema_and_backend_both_reject_missing_implementation():
    from jsonschema import Draft202012Validator, ValidationError as SchemaError
    from pydantic import ValidationError
    from app.application.learning_agents import SkillXDraft
    draft = responses()["SkillXDraft"]
    draft["skillx"].pop("source")
    with pytest.raises(SchemaError):
        Draft202012Validator(SkillXDraft.model_json_schema()).validate(draft)
    with pytest.raises(ValidationError, match="source"):
        SkillXDraft.model_validate(draft)


def test_schema_feedback_locates_misnested_code_without_echoing_it():
    from app.application.agent_host import output_schema_feedback
    from app.application.learning_agents import SkillXDraft
    draft = responses()["SkillXDraft"]
    draft["skillx"]["outputSchema"]["source"] = draft["skillx"].pop("source")
    feedback = output_schema_feedback(SkillXDraft.model_json_schema(), draft)
    assert "skillx.outputSchema.source" in feedback
    assert "修正 JSON 层级" in feedback
    assert "PRIVATE-SOURCE" not in feedback


def test_workflow_builder_schema_matches_executable_node_contract():
    from jsonschema import Draft202012Validator, ValidationError as SchemaError
    from pydantic import ValidationError
    from app.application.learning_agents import WorkflowDraft
    value = responses()["WorkflowDraft"]
    value["workflow"]["spec"]["nodes"].append("placeholder")
    with pytest.raises(SchemaError):
        Draft202012Validator(WorkflowDraft.model_json_schema()).validate(value)
    with pytest.raises(ValidationError, match="nodes"):
        WorkflowDraft.model_validate(value)


async def test_professional_roles_have_separate_sessions_schemas_and_scoped_evidence(monkeypatch):
    worker = await pipeline()
    requests, order = [], []
    mock_agents(monkeypatch, responses(), requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert order == [
        "ExtractionPlan",
        "SkillDraft",
        "SkillXDraft",
        "validate:calculate",
        "WorkflowDraft",
    ]
    assert len({request.run_id for request in requests}) == 4
    assert len({request.working_directory for request in requests}) == 4
    for request, marker in zip(
        requests, ["提炼专家", "指令整理专家", "契约与实现构建专家", "编排专家"], strict=True
    ):
        assert marker in request.prompt
    assert "WHOLE-TASK" in requests[0].prompt
    for request, id in zip(requests[1:], ["guide-call", "script-call", "flow-call"], strict=True):
        assert [call["id"] for call in data(request)["calls"]] == [id]
        assert "WHOLE-TASK" not in request.prompt
        assert "unrelated-call" not in request.prompt
    assert "PRIVATE-SOURCE" not in requests[-1].prompt
    assert "PRIVATE-GUIDE" not in requests[-1].prompt
    assert data(requests[-1])["suppliedSkillXContracts"][0]["validationStatus"] == "passed"
    assert proposal.workflows[0].scope == "可重复利用的独立片段"
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        stages = job.result_json["agentStages"]
        assert set(stages) == {
            "extractor",
            "skill:guide",
            "skillx:calculate",
            "workflow:fixed-flow",
        }
        assert all(stage["status"] == "completed" for stage in stages.values())
        assert stages["skillx:calculate"]["validationStatus"] == "passed"
        events = list(
            await session.scalars(select(TaskEvent).where(TaskEvent.task_id == worker.task_id))
        )
        assert len([event for event in events if event.type == "learning.agent.audit"]) == 4
        assert not any(event.type == "agent.audit" for event in events)


async def test_completed_stages_survive_worker_restart_and_only_invalidated_stage_repeats(
    monkeypatch,
):
    worker = await pipeline()
    requests, order = [], []
    values = responses()
    mock_agents(monkeypatch, values, requests, order)
    await worker.generate(context(), materializer=materializer(order))
    await worker.invalidate("workflow", "fixed-flow", "图输出与预期结果不一致")
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.lease_token = "replacement-worker"
    restarted = LearningAgentPipeline(worker.job_id, worker.task_id, "replacement-worker", None)
    await restarted.generate(context(), materializer=materializer(order))
    assert order == [
        "ExtractionPlan",
        "SkillDraft",
        "SkillXDraft",
        "validate:calculate",
        "WorkflowDraft",
        "WorkflowDraft",
    ]
    feedback = data(requests[-1])["validationFeedback"]
    assert feedback["error"] == "图输出与预期结果不一致"
    assert feedback["previousDraft"]["workflow"]
    assert requests[-1].run_id != requests[-2].run_id


async def test_failed_script_validation_retries_only_skillx_builder(monkeypatch):
    worker = await pipeline()
    values = responses()
    values["ExtractionPlan"]["skills"] = []
    values["ExtractionPlan"]["workflow"] = None
    values["SkillXRepair"] = {"source": values["SkillXDraft"]["skillx"]["source"] + "\n# repaired"}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    failed = True

    async def verify(skill, ids):
        if failed:
            raise ValueError("执行结果与业务断言不一致")
        return await materializer(order)(skill, ids)

    ctx = context()
    ctx["calls"][1]["observedOutput"] = {"nested": {"actual": "proof"}}
    proposal = await worker.generate(ctx, materializer=verify)
    assert "业务断言" in proposal.failures["skillx:calculate"]
    failed = False
    await worker.generate(context(), materializer=verify)
    assert order == ["ExtractionPlan", "SkillXDraft", "SkillXRepair", "validate:calculate"]
    assert data(requests[-1])["validationFeedback"] == "执行结果与业务断言不一致"

    evidence = data(requests[-1])["calls"]
    assert [call["id"] for call in evidence] == ["script-call"]
    assert evidence[0]["observedOutput"] == {"nested": {"actual": "proof"}}


async def test_invalid_source_is_returned_to_builder_for_one_targeted_revision(monkeypatch):
    worker = await pipeline()
    values = responses()
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    execute = AgentHost.execute
    original = values["SkillXDraft"]["skillx"]["source"]
    async def revise(self, request, **kwargs):
        if request.output_schema["title"] == "SkillXImplementation":
            inputs = data(request)
            assert inputs["validationFeedback"]["previousDraft"]["skillx"]["source"] == ""
            assert "source" in inputs["validationFeedback"]["error"]
            requests.append(request)
            order.append("SkillXImplementation")
            return AgentHostResult(output={"source": original, "examples": values["SkillXDraft"]["skillx"]["examples"]})
        if request.output_schema["title"] == "SkillXDraft":
            inputs = data(request)
            assert "class SkillXRequest" in inputs["runtimeContract"]["requestType"]
            if "validationFeedback" not in inputs:
                values["SkillXDraft"]["skillx"]["source"] = ""
            else:
                assert "source" in inputs["validationFeedback"]["error"]
                assert inputs["validationFeedback"]["previousDraft"]["skillx"]["source"] == ""
                values["SkillXDraft"]["skillx"]["source"] = original
        return await execute(self, request, **kwargs)
    monkeypatch.setattr(AgentHost, "execute", revise)
    result = await worker.generate(context(), materializer=materializer(order))
    assert not result.failures
    assert len(result.skills) == len(result.skillx) == len(result.workflows) == 1
    assert order.count("SkillXDraft") == 1
    assert order.count("SkillXImplementation") == 1
    assert order.count("ExtractionPlan") == order.count("SkillDraft") == 1


async def test_rejected_dependency_does_not_dispatch_workflow_builder(monkeypatch):
    worker = await pipeline()
    values = responses()
    values["ExtractionPlan"]["skills"] = []
    values["SkillXDraft"] = {"decision": "skip", "reason": "没有可验证的规则", "skillx": None}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert proposal.decision == "candidate"
    assert "依赖未构建" in proposal.failures["workflow:fixed-flow"]
    assert order == ["ExtractionPlan", "SkillXDraft"]


async def test_missing_workflow_examples_are_completed_without_regenerating_graph(monkeypatch):
    worker = await pipeline()
    values = responses()
    values["WorkflowDraft"]["workflowExamples"] = []
    values["WorkflowExamples"] = {"workflowExamples": [
        {"input": {"value": 1}, "expected": {"value": 1}, "sourceInvocationId": "flow-call"},
        {"input": {"value": 2}, "expected": {"value": 2}},
    ]}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    result = await worker.generate(context(), materializer=materializer(order))
    assert not result.failures
    assert result.workflows[0].definition == values["WorkflowDraft"]["workflow"]
    assert len(result.workflows[0].examples) == 2
    assert order.count("WorkflowDraft") == order.count("WorkflowExamples") == 1
    assert order.count("SkillXDraft") == 1


async def test_missing_mappings_are_wired_by_agent_without_rebuilding_nodes(monkeypatch):
    from copy import deepcopy
    worker = await pipeline()
    values = responses()
    original = deepcopy(values["WorkflowDraft"]["workflow"])
    mappings = {node["id"]: node.get("input", {}) for node in original["spec"]["nodes"]}
    for node in values["WorkflowDraft"]["workflow"]["spec"]["nodes"]:
        node.pop("input", None)
    values["WorkflowBindings"] = {"nodeInputs": mappings}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    result = await worker.generate(context(), materializer=materializer(order))
    assert not result.failures
    assert result.workflows[0].definition["spec"]["edges"] == original["spec"]["edges"]
    assert result.workflows[0].definition["spec"]["nodes"][1]["input"] == mappings["format"]
    assert order.count("WorkflowDraft") == order.count("WorkflowBindings") == 1
    await worker.invalidate("workflow", "fixed-flow", "隔离验证发现绑定范围错误")
    await worker._record("workflow:fixed-flow:bindings", {
        "status": "needs_revision", "error": "隔离验证发现绑定范围错误"})
    again = await worker.generate(context(), materializer=materializer(order))
    assert not again.failures
    assert order.count("WorkflowDraft") == 1
    assert order.count("WorkflowBindings") == 2


async def test_lost_lease_prevents_model_dispatch(monkeypatch):
    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.lease_token = "other-worker"
    requests, order = [], []
    mock_agents(monkeypatch, responses(), requests, order)
    with pytest.raises(LearningLeaseLost):
        await worker.generate(context(), materializer=materializer(order))
    assert requests == []


async def test_specialist_cannot_expand_its_evidence_scope(monkeypatch):
    worker = await pipeline()
    values = responses()
    values["SkillDraft"]["skill"]["sourceInvocationIds"] = ["unrelated-call"]
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert "范围内" in proposal.failures["skill:guide"]
    assert len(proposal.skillx) == 1
    assert len(proposal.workflows) == 1


def test_extractor_cannot_route_unknown_dependencies_or_fabricated_calls():
    value = responses()["ExtractionPlan"]
    value["workflow"]["dependsOn"] = ["unknown"]
    with pytest.raises(ValueError, match="依赖"):
        validate_extraction(ExtractionPlan.model_validate(value), context())
    value["workflow"]["dependsOn"] = ["calculate"]
    value["skills"][0]["sourceInvocationIds"] = ["invented"]
    with pytest.raises(ValueError, match="执行证据"):
        validate_extraction(ExtractionPlan.model_validate(value), context())


def test_builder_context_keeps_interfaces_but_omits_dependency_programs():
    from app.application.learning_agents import compact_builder_context
    original = {"catalog": {"huge": "PRIVATE-CATALOG"}, "workflowExample": {"irrelevant": True},
        "suppliedSkillXContracts": [{"name": "git", "executionProfile": "local-command",
            "inputSchema": {"required": ["repoPath"]}, "examples": [{"setup": "PRIVATE-SETUP"}]}],
        "calls": [{"id": "proof", "status": "succeeded", "input": {}, "output": "x" * 10000}]}
    compact = compact_builder_context(original)
    assert compact["suppliedSkillXContracts"][0]["inputSchema"] == {"required": ["repoPath"]}
    assert "PRIVATE-SETUP" not in json.dumps(compact)
    assert "PRIVATE-CATALOG" not in json.dumps(compact)
    assert len(json.dumps(compact)) < 2000
    assert original["suppliedSkillXContracts"][0]["examples"]


async def test_revision_checkpoint_invalidates_only_dependency_closure_and_is_idempotent():
    from app.application.learning_revisions import create_revision_job
    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        source = await session.get(LearningJob, worker.job_id)
        source.result_json = {"agentStages": {
            "extractor": {"output": {"workflows": [
                {"name": "child", "dependsOn": ["broken"]},
                {"name": "parent", "dependsOn": ["child"]},
                {"name": "unrelated", "dependsOn": ["good"]}]}},
            "skillx:broken": {"status": "completed", "validationStatus": "passed", "output": {"source": "preserved"}},
            "skillx:good": {"status": "completed", "validationStatus": "passed"}},
            "artifacts": {"skillx": {"broken": "v1", "good": "v2"},
                "workflows": {"child": "w1", "parent": "w2", "unrelated": "w3"}}}
        revised = await create_revision_job(session, source, {"skillx:broken": "cwd mismatch"})
        same = await create_revision_job(session, source, {"skillx:broken": "cwd mismatch"})
        assert same.id == revised.id
        assert revised.result_json["artifacts"]["skillx"] == {"good": "v2"}
        assert revised.result_json["artifacts"]["workflows"] == {"unrelated": "w3"}
        assert revised.result_json["revisionBaseWorkflowVersions"] == {"child": "w1", "parent": "w2"}
        assert revised.result_json["agentStages"]["skillx:broken"]["output"] == {"source": "preserved"}
        assert source.result_json["artifacts"]["skillx"]["broken"] == "v1"


@pytest.mark.parametrize("source", ["implementation omitted", "# code", "def execute("])
def test_python_semantics_rejected_by_every_builder_contract(source):
    from app.application.learning_agents import SkillXDraft, SkillXImplementation, SkillXRepair
    from pydantic import ValidationError
    value = responses()["SkillXDraft"]
    value["skillx"]["source"] = source
    for model, payload in [(SkillXDraft, value),
        (SkillXImplementation, {"source": source, "examples": value["skillx"]["examples"]}),
        (SkillXRepair, {"source": source})]:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_source_transport_schema_allows_complete_multiline_python_without_regex():
    from app.application.learning_agents import SkillXDraft, SkillXImplementation, SkillXRepair
    from jsonschema import Draft202012Validator
    source = "import os\n\ndef execute(request, context):\n    return request.input\n"
    value = responses()["SkillXDraft"]
    value["skillx"]["source"] = source
    for model, payload in [(SkillXDraft, value),
        (SkillXImplementation, {"source": source, "examples": value["skillx"]["examples"]}),
        (SkillXRepair, {"source": source})]:
        schema = model.model_json_schema()
        def visit(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key == "source" and isinstance(child, dict):
                        assert "pattern" not in json.dumps(child)
                    visit(child)
            elif isinstance(node, list):
                for child in node: visit(child)
        visit(schema)
        Draft202012Validator(schema).validate(payload)
        model.model_validate(payload)


def test_invalid_workflow_dependencies_do_not_block_independent_graph():
    from app.application.learning_agents import WorkflowFragment, independent_workflows
    def flow(name, dependencies):
        return WorkflowFragment(name=name, description=name, scope=name,
            useWhen=[name], sourceInvocationIds=['call'], dependsOn=dependencies)
    ordered, failures = independent_workflows([
        flow('broken', ['typo']), flow('child', ['atomic']),
        flow('parent', ['child']), flow('blocked-parent', ['broken']),
        flow('cycle-a', ['cycle-b']), flow('cycle-b', ['cycle-a'])], {'atomic'})
    assert [item.name for item in ordered] == ['child', 'parent']
    assert set(failures) == {'workflow:broken', 'workflow:blocked-parent', 'workflow:cycle-a', 'workflow:cycle-b'}


async def test_dependency_binding_uses_finite_names_without_regenerating_plan(monkeypatch):
    worker = await pipeline()
    values = responses()
    values['ExtractionPlan']['workflow']['dependsOn'] = ['calculate-typo']
    values['ExtractionDependencyBindings'] = {'fixed-flow': ['calculate']}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert not proposal.failures
    assert order.count('ExtractionPlan') == 1
    assert order.count('SkillXDraft') == 1
    binding = next(r for r in requests if r.output_schema['title'] == 'ExtractionDependencyBindings')
    assert binding.output_schema['properties']['fixed-flow']['items']['const'] == 'calculate'
    assert proposal.workflows[0].dependsOn == ['calculate']


async def test_failed_dependency_binding_preserves_independent_skillx(monkeypatch):
    worker = await pipeline()
    values = responses()
    values['ExtractionPlan']['workflow']['dependsOn'] = ['missing']
    values['ExtractionDependencyBindings'] = {'fixed-flow': ['still-missing']}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert [item.name for item in proposal.skillx] == ['calculate']
    assert 'workflow:fixed-flow' in proposal.failures
    await worker.generate(context(), materializer=materializer(order))
    assert order.count('ExtractionDependencyBindings') == 1


def test_nested_business_schema_is_typed_in_transport_and_backend():
    from app.application.learning_agents import SkillXDraft
    from jsonschema import Draft202012Validator, ValidationError
    from pydantic import ValidationError as PydanticError
    value = responses()['SkillXDraft']
    value['skillx']['inputSchema']['properties']['value'] = 'in-puts'
    with pytest.raises(ValidationError):
        Draft202012Validator(SkillXDraft.model_json_schema()).validate(value)
    with pytest.raises(PydanticError):
        SkillXDraft.model_validate(value)


async def test_invalid_business_schema_reaches_agent_delta_repair(monkeypatch):
    worker = await pipeline()
    values = responses()
    values['ExtractionPlan']['skills'] = []
    values['ExtractionPlan']['workflow'] = None
    good = json.loads(json.dumps(values['SkillXDraft']['skillx']['inputSchema']))
    values['SkillXDraft']['skillx']['inputSchema'] = json.loads(json.dumps(good))
    values['SkillXDraft']['skillx']['inputSchema']['properties']['value'] = 'in-puts'
    values['SkillXRepair'] = {'inputSchema': good}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert not proposal.failures
    assert proposal.skillx[0].inputSchema == good
    assert order == ['ExtractionPlan', 'SkillXDraft', 'SkillXRepair', 'validate:calculate']
    repair = data(requests[-1])
    assert repair['previousDraft']['inputSchema']['properties']['value'] == 'in-puts'
    assert 'inputSchema' in repair['validationFeedback']


def test_business_schema_omits_optional_keywords_but_preserves_const_null():
    from app.application.learning_contracts import validate_business_schema, BusinessSchemaDefinition
    schema = {'type': 'object', 'items': None, 'properties': {'value': {'const': None, 'items': None}}}
    result = validate_business_schema(BusinessSchemaDefinition.model_validate(schema))
    assert result == {'type': 'object', 'properties': {'value': {'const': None}}}


def test_local_fixture_provenance_comes_from_scoped_host_evidence():
    from types import SimpleNamespace
    from app.application.learning_contracts import ContractExample
    from app.application.learning_evidence import validate_examples
    examples = [ContractExample(input={'value': value}, expected={}, setup='pass', verify='assert True')
                for value in [1, 2]]
    validate_examples(examples, {'scoped': SimpleNamespace(status='succeeded')}, local_command=True)
    for evidence in [{}, {'scoped': SimpleNamespace(status='failed')}]:
        with pytest.raises(ValueError, match='缺少关联'):
            validate_examples(examples, evidence, local_command=True)
    examples[0].sourceInvocationId = 'invented'
    with pytest.raises(ValueError, match='不存在'):
        validate_examples(examples, {'scoped': SimpleNamespace(status='succeeded')}, local_command=True)


def test_empty_input_contract_rejected_before_running_generated_code():
    from app.application.learning_agents import SkillXDraft
    from pydantic import ValidationError
    value = responses()['SkillXDraft']
    value['skillx']['inputSchema'] = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    with pytest.raises(ValidationError, match='inputSchema 不一致'):
        SkillXDraft.model_validate(value)


@pytest.mark.parametrize('keyword', ['allOf', 'anyOf', 'oneOf'])
def test_empty_schema_combinators_rejected_at_transport(keyword):
    from app.application.learning_agents import SkillXDraft
    from jsonschema import Draft202012Validator, ValidationError
    value = responses()['SkillXDraft']
    value['skillx']['inputSchema']['properties']['value'] = {keyword: []}
    with pytest.raises(ValidationError):
        Draft202012Validator(SkillXDraft.model_json_schema()).validate(value)


def test_local_reuse_cases_include_repository_state_as_input():
    from types import SimpleNamespace
    from app.application.learning_contracts import ContractExample
    from app.application.learning_evidence import validate_examples
    examples = [ContractExample(input={'repoPath': 'repo'}, expected={}, setup=setup, verify='assert True')
                for setup in ['create_clean_repository()', 'create_dirty_repository()']]
    evidence = {'source': SimpleNamespace(status='succeeded')}
    validate_examples(examples, evidence, local_command=True)
    examples[1].setup = examples[0].setup
    with pytest.raises(ValueError, match='至少两个'):
        validate_examples(examples, evidence, local_command=True)


async def test_publication_precedes_dependent_generation(monkeypatch):
    from copy import deepcopy
    worker = await pipeline()
    values = responses()
    child = fragment("child", "flow-call", dependsOn=["calculate"])
    parent = fragment("parent", "flow-call", dependsOn=["child"])
    values["ExtractionPlan"].update(skills=[], workflow=None, workflows=[parent, child])
    order = []
    async def execute(self, request, **kwargs):
        role = request.output_schema["title"]
        inputs = data(request)
        if role != "WorkflowDraft":
            return AgentHostResult(output=values[role])
        name = inputs["fragment"]["name"]
        order.append("generate:" + name)
        draft = deepcopy(values[role])
        if name == "parent":
            assert order.index("publish:child") < order.index("generate:parent")
            assert inputs["suppliedWorkflowContracts"][0]["outputSchema"]["description"] == "Agent-repaired contract"
            draft["workflow"]["spec"]["nodes"][1].update(
                type="workflow_call", input={"name": "${input.name}"},
                config={"generatedWorkflow": "child"})
        return AgentHostResult(output=draft)
    monkeypatch.setattr(AgentHost, "execute", execute)
    artifacts = {"skills": {}, "skillx": {}, "workflows": {}}
    async def publish(proposal):
        group = "skillx" if proposal.skillx else "workflows"
        item = getattr(proposal, group)[0]
        order.append("publish:" + item.name)
        artifacts[group][item.name] = "version"
        if item.name == "child":
            stage = await worker._stage("workflow:child")
            stage["output"]["workflow"]["spec"]["outputSchema"]["description"] = "Agent-repaired contract"
            await worker._record("workflow:child", stage)
        return "published", {"artifacts": artifacts, "failures": {}}
    result = await worker.generate(context(), materializer=materializer(order), publish=publish)
    assert not result.failures
    assert order == ["validate:calculate", "publish:calculate", "generate:child", "publish:child", "generate:parent", "publish:parent"]


async def test_extraction_budget_is_job_scoped_and_survives_resume(monkeypatch):
    from datetime import UTC
    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {"budget": {"maxDurationSeconds": 1200, "maxTokens": 200000,
            "startedAt": utcnow().isoformat()}, "usageByRequest": {"old-call": 199000}}
    async def unexpected(*args, **kwargs):
        pytest.fail("Budget exhausted job must not call Agent")
    monkeypatch.setattr(AgentHost, "execute", unexpected)
    with pytest.raises(ValueError, match="预算不足"):
        await worker.call("extractor", ExtractionPlan, context())
    other = await pipeline()
    async with SessionLocal() as session:
        assert "budget" not in (await session.get(LearningJob, other.job_id)).result_json


@pytest.mark.parametrize('profile', [None, 'pure-python', 'local-command'])
def test_execution_profile_and_fixtures_share_generation_contract(profile):
    from jsonschema import Draft202012Validator, ValidationError as SchemaError
    from pydantic import ValidationError
    from app.application.learning_agents import SkillXDraft
    value = responses()['SkillXDraft']
    skill = value['skillx']
    if profile is None:
        skill.pop('executionProfile')
    else:
        skill['executionProfile'] = profile
    for example in skill['examples']:
        example.update(setup='x = 1', verify='assert result')
    if profile == 'local-command':
        Draft202012Validator(SkillXDraft.model_json_schema()).validate(value)
        assert SkillXDraft.model_validate(value).skillx.executionProfile == profile
    else:
        with pytest.raises(SchemaError):
            Draft202012Validator(SkillXDraft.model_json_schema()).validate(value)
        with pytest.raises(ValidationError):
            SkillXDraft.model_validate(value)


async def test_agent_can_repair_missing_execution_profile_without_regenerating_code(monkeypatch):
    from app.application.learning_agents import ReusableFragment
    worker = await pipeline()
    value = responses()['SkillXDraft']
    value['skillx'].pop('executionProfile')
    original = value['skillx']['source']
    await worker._record('skillx:calculate', {'role': 'skillx', 'output': value,
        'status': 'failed', 'error': 'executionProfile missing'})
    async def execute(self, request, **kwargs):
        assert 'executionProfile' in request.output_schema['properties']
        return AgentHostResult(output={'executionProfile': 'pure-python'})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    draft = await worker.repair_skillx(ReusableFragment.model_validate(fragment('calculate', 'script-call')),
        {'output': value, 'error': 'executionProfile missing'}, lambda value: None)
    assert draft.skillx.executionProfile == 'pure-python'
    assert draft.skillx.source == original


async def test_explicit_composition_requirement_is_checked_before_any_builder(monkeypatch):
    from app.application.learning_agents import ComposedExtractionPlan
    worker = await pipeline()
    ctx = context()
    ctx['requirements'] = {'requireWorkflowComposition': True}
    count = 0
    async def execute(self, request, **kwargs):
        nonlocal count
        count += 1
        assert request.output_schema['title'] == 'ComposedExtractionPlan'
        if count == 1:
            return AgentHostResult(output=responses()['ExtractionPlan'])
        assert '独立工作流' in data(request)['validationFeedback']['error']
        return AgentHostResult(output={'decision': 'skip', 'reason': '来源不足，不能虚构组合'})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    result = await worker.generate(ctx, materializer=materializer([]))
    assert result.decision == 'skip'
    assert count == 2
    # Simple extraction remains valid without forcing artificial hierarchy.
    assert ExtractionPlan.model_validate(responses()['ExtractionPlan'])


def test_ready_workflow_examples_are_required_by_schema_and_backend():
    from app.application.learning_agents import WorkflowDraft
    from jsonschema import Draft202012Validator, ValidationError as SchemaError
    from pydantic import ValidationError
    value = responses()['WorkflowDraft']
    value.pop('workflowExamples')
    with pytest.raises(SchemaError):
        Draft202012Validator(WorkflowDraft.model_json_schema()).validate(value)
    with pytest.raises(ValidationError):
        WorkflowDraft.model_validate(value)


def test_workflow_generation_schema_rejects_compiler_errors_and_missing_bindings():
    from copy import deepcopy
    from jsonschema import Draft202012Validator, ValidationError as SchemaError
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema
    schema = bind_workflow_schema(WorkflowDraft.model_json_schema(), {'suppliedSkillXContracts': [
        {'name': 'calculate', 'inputSchema': {'required': ['value']}}]})
    valid = responses()['WorkflowDraft']
    validator = Draft202012Validator(schema)
    validator.validate(valid)
    for mode in ['mapping', 'output', 'retry']:
        value = deepcopy(valid)
        nodes = value['workflow']['spec']['nodes']
        if mode == 'mapping': nodes[1]['input'] = {}
        if mode == 'output': nodes.pop()
        if mode == 'retry': nodes[1].update(sideEffect=True, idempotent=False, retry={'maxAttempts': 2})
        with pytest.raises(SchemaError): validator.validate(value)


def test_workflow_context_preserves_existing_catalog_even_with_empty_new_dependencies():
    from app.application.learning_agents import compact_builder_context
    contract = {"id": "published-version", "digest": "sha256:verified", "interfaces": {
        "execute": {"inputSchema": {"required": ["repo"]}, "outputSchema": {"type": "object"}}}}
    data = {"catalog": {"capabilities": [contract], "tools": [{"large": "not needed"}]},
            "suppliedSkillXContracts": [], "suppliedWorkflowContracts": []}
    assert compact_builder_context(data)["catalog"] == {"capabilities": [contract]}

@pytest.mark.parametrize('host_rejection', [False, True])
async def test_nested_workflow_examples_are_repaired_by_agent_once(monkeypatch, host_rejection):
    from copy import deepcopy
    from app.application.agent_host import AgentHostError
    worker = await pipeline()
    values = responses()
    valid = deepcopy(values['WorkflowDraft'])
    malformed = values['WorkflowDraft']
    malformed['workflow']['workflowExamples'] = malformed.pop('workflowExamples')
    values['WorkflowRepair'] = {key: valid[key] for key in ('workflow', 'workflowExamples')}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    execute = AgentHost.execute

    async def run(self, request, **kwargs):
        role = request.output_schema['title']
        if role == 'WorkflowRepair':
            payload = data(request)
            assert payload['previousDraft']['workflow']['workflowExamples'] == valid['workflowExamples']
            assert 'workflowExamples' in payload['validationFeedback']['error']
            assert payload['catalog']['capabilities'] == [{'id': 'existing', 'name': 'published'}]
        result = await execute(self, request, **kwargs)
        if role == 'WorkflowDraft' and host_rejection:
            await kwargs['event_sink']({'type': 'agent.completed', 'payload': {'output': result.output}}, {})
            raise AgentHostError('CONTRACT_VIOLATION', 'workflow.workflowExamples forbidden')
        return result

    monkeypatch.setattr(AgentHost, 'execute', run)
    ctx = context()
    ctx['catalog']['capabilities'] = [{'id': 'existing', 'name': 'published'}]
    result = await worker.generate(ctx, materializer=materializer(order))
    assert not result.failures
    assert result.workflows[0].definition == valid['workflow']
    assert order.count('WorkflowDraft') == order.count('WorkflowRepair') == 1
    assert (await worker._stage('workflow:fixed-flow'))['revisionCount'] == 1
    assert await worker.revise_workflow(result.workflows[0], 'new failure') is None

@pytest.mark.parametrize('complete_change', [False, True])
async def test_skillx_revision_requires_consistent_examples_schema_and_code(monkeypatch, complete_change):
    worker = await pipeline()
    values = responses()
    values['ExtractionPlan']['skills'] = []
    values['ExtractionPlan']['workflow'] = None
    patch = {'examples': [
        {'input': {'amount': 1}, 'expected': {'value': 1}, 'sourceInvocationId': 'script-call'},
        {'input': {'amount': 2}, 'expected': {'value': 2}},
    ]}
    if complete_change:
        patch.update(inputSchema={'type': 'object', 'properties': {'amount': {'type': 'integer'}},
                                  'required': ['amount'], 'additionalProperties': False},
                     source='def execute(request, context): return {"value": request.input["amount"]}')
    values['SkillXRepair'] = patch
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)

    async def verify(skill, ids):
        if 'amount' not in skill.inputSchema['required']:
            raise ValueError('输入契约需要与实际业务参数同步')
        assert 'request.input["amount"]' in skill.source
        return await materializer(order)(skill, ids)

    result = await worker.generate(context(), materializer=verify)
    assert order.count('SkillXDraft') == order.count('SkillXRepair') == 1
    if complete_change:
        assert not result.failures
        assert result.skillx[0].inputSchema['required'] == ['amount']
    else:
        assert 'required property' in result.failures['skillx:calculate']
        assert not result.skillx

@pytest.mark.parametrize('node_type, ref_key', [('capability_call', 'capabilityVersionId'), ('workflow_call', 'workflowVersionId')])
def test_catalog_local_dependencies_require_fixtures_in_generated_schema(node_type, ref_key):
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema, workflow_uses_local_validation
    from jsonschema import Draft202012Validator
    value = responses()['WorkflowDraft']
    value['workflow']['spec']['nodes'][1].update(type=node_type,
        config={ref_key: 'published-local', 'interface': 'execute', 'digest': 'sha256:local'})
    inputs = {'suppliedSkillXContracts': [], 'suppliedWorkflowContracts': [], 'catalog': {'capabilities': [
        {'id': 'published-local', 'interfaces': {'execute': {'requiresLocalValidation': True}}}]}}
    assert workflow_uses_local_validation(value['workflow'], inputs)
    schema = bind_workflow_schema(WorkflowDraft.model_json_schema(), inputs)
    assert list(Draft202012Validator(schema).iter_errors(value))
    for example in value['workflowExamples']:
        example.update(setup='create_temporary_repository()', verify='assert state_matches(result)')
    assert not list(Draft202012Validator(schema).iter_errors(value))
    value['workflow']['spec']['nodes'][1]['config'][ref_key] = 'unrelated-pure'
    assert not workflow_uses_local_validation(value['workflow'], inputs)

@pytest.mark.parametrize('invalid', [[{'input': {'x': 1}, 'output': {'x': 1}}], [], ['  ']])
def test_publication_metadata_examples_share_model_and_backend_contract(invalid):
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema
    from app.application.workflow_catalog import capability_metadata_issues
    from app.domain.workflow import WorkflowDefinition
    from jsonschema import Draft202012Validator
    value = responses()['WorkflowDraft']
    schema = bind_workflow_schema(WorkflowDraft.model_json_schema(), {})
    assert not list(Draft202012Validator(schema).iter_errors(value))
    value['workflow']['metadata']['capability']['examples'] = invalid
    assert list(Draft202012Validator(schema).iter_errors(value))
    with pytest.raises(ValueError, match='metadata.capability'):
        WorkflowDraft.model_validate(value)
    assert capability_metadata_issues(WorkflowDefinition.model_validate(value['workflow'])) == ['metadata.capability.examples']


async def test_bad_metadata_returns_exact_type_error_for_agent_revision(monkeypatch):
    from copy import deepcopy
    worker = await pipeline()
    values = responses()
    valid = deepcopy(values['WorkflowDraft'])
    values['WorkflowDraft']['workflow']['metadata']['capability']['examples'] = [{'input': {}, 'output': {}}]
    values['WorkflowRepair'] = {'workflow': valid['workflow']}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    result = await worker.generate(context(), materializer=materializer(order))
    assert not result.failures
    feedback = data(requests[-1])['validationFeedback']['error']
    assert 'metadata.capability' in feedback and 'examples.0' in feedback and 'string' in feedback
    assert order.count('WorkflowRepair') == 1
    assert result.workflows[0].definition['metadata']['capability']['examples'] == ['输入 1，返回 1']

@pytest.mark.parametrize('name', [None, '', '   '])
def test_complete_publication_metadata_contract_rejects_missing_name_before_execution(name):
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema
    from app.application.workflow_catalog import capability_metadata_issues
    from app.domain.workflow import WorkflowDefinition
    from jsonschema import Draft202012Validator
    value = responses()['WorkflowDraft']
    if name is None: value['workflow']['metadata'].pop('name')
    else: value['workflow']['metadata']['name'] = name
    schema = bind_workflow_schema(WorkflowDraft.model_json_schema(), {})
    assert list(Draft202012Validator(schema).iter_errors(value))
    with pytest.raises(ValueError, match='metadata.name'):
        WorkflowDraft.model_validate(value)
    assert capability_metadata_issues(WorkflowDefinition.model_validate(value['workflow'])) == ['metadata.name']


async def test_missing_metadata_name_is_repaired_with_precise_feedback(monkeypatch):
    from copy import deepcopy
    worker = await pipeline()
    values = responses()
    valid = deepcopy(values['WorkflowDraft'])
    values['WorkflowDraft']['workflow']['metadata'].pop('name')
    values['WorkflowRepair'] = {'workflow': valid['workflow']}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    result = await worker.generate(context(), materializer=materializer(order))
    assert not result.failures
    assert 'metadata.name: Field required' in data(requests[-1])['validationFeedback']['error']
    assert order.count('WorkflowRepair') == 1
    assert result.workflows[0].definition['metadata']['name'] == valid['workflow']['metadata']['name']

@pytest.mark.parametrize('payload', [
    {'usageComplete': False, 'inputTokens': 0, 'outputTokens': 0},
    {'usageComplete': True, 'inputTokens': 10},
])
async def test_unknown_builder_usage_blocks_next_call_within_same_job_only(monkeypatch, payload):
    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {'budget': {'maxDurationSeconds': 1200, 'maxTokens': 200000,
                                     'startedAt': utcnow().isoformat()}}
    calls = []
    async def execute(self, request, **kwargs):
        calls.append(request.run_id)
        await kwargs['event_sink']({}, {'type': 'agent.step.completed', 'payload': {
            'requestId': 'unknown-provider-call', **payload}})
        return AgentHostResult(output={'decision': 'skip', 'reason': 'none'})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    await worker.call('extractor', ExtractionPlan, context())
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        assert job.result_json['validationUsageIncomplete'] is True
        assert 'unknown-provider-call' not in job.result_json['usageByRequest']
    with pytest.raises(ValueError, match='用量不完整'):
        await worker.call('extractor', ExtractionPlan, {**context(), 'goal': 'new phase'})
    assert len(calls) == 1
    fresh = await pipeline()
    await fresh.call('extractor', ExtractionPlan, context())
    assert len(calls) == 2


async def test_replayed_complete_step_usage_is_counted_once(monkeypatch):
    worker = await pipeline()
    async def execute(self, request, **kwargs):
        event = {'type': 'agent.step.completed', 'payload': {
            'requestId': 'provider-call', 'usageComplete': True, 'inputTokens': 123, 'outputTokens': 45}}
        await kwargs['event_sink']({}, event)
        await kwargs['event_sink']({}, event)
        return AgentHostResult(output={'decision': 'skip', 'reason': 'none'})
    monkeypatch.setattr(AgentHost, 'execute', execute)
    await worker.call('extractor', ExtractionPlan, context())
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        assert job.result_json['usageByRequest'] == {'provider-call': 168}
        assert not job.result_json.get('validationUsageIncomplete')

@pytest.mark.parametrize('child_failed', [False, True])
async def test_parent_agent_can_explicitly_reuse_published_skipped_dependency_only(monkeypatch, child_failed):
    from copy import deepcopy
    from app.application.agent_host import AgentHostError
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema
    from jsonschema import Draft202012Validator
    worker = await pipeline()
    values = responses()
    values['ExtractionPlan'].update(skills=[], skillx=[], workflow=None, workflows=[
        fragment('redundant-child', 'flow-call'), fragment('parent', 'flow-call', dependsOn=['redundant-child'])])
    parent = deepcopy(values['WorkflowDraft'])
    parent['workflow']['spec']['nodes'][1]['config'] = {
        'capabilityVersionId': 'published', 'interface': 'execute', 'digest': 'sha256:published'}
    parent['workflow']['spec']['nodes'][1]['type'] = 'capability_call'
    parent['reusedDependencies'] = {'redundant-child': {'versionId': 'published', 'interface': 'execute',
        'digest': 'sha256:published', 'reason': 'Existing atomic capability already implements this stage'}}
    ctx = context()
    ctx['catalog']['capabilities'] = [{'id': 'published', 'name': 'existing', 'digest': 'sha256:published',
        'interfaces': {'execute': {'inputSchema': {'required': ['value']}, 'outputSchema': {}}}}]
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    execute = AgentHost.execute
    async def agent(self, request, **kwargs):
        if request.output_schema['title'] == 'WorkflowDraft':
            args = data(request)
            if args['fragment']['name'] == 'redundant-child':
                if child_failed:
                    raise AgentHostError('EXECUTION_FAILED', 'actual builder failure')
                values['WorkflowDraft'] = {'decision': 'skip', 'reason': 'Already available as published atomic capability'}
            else:
                assert args['skippedDependencies'][0]['name'] == 'redundant-child'
                assert args['suppliedWorkflowContracts'] == []
                values['WorkflowDraft'] = parent
                assert not list(Draft202012Validator(request.output_schema).iter_errors(parent))
        return await execute(self, request, **kwargs)
    monkeypatch.setattr(AgentHost, 'execute', agent)
    result = await worker.generate(ctx, materializer=materializer(order))
    if child_failed:
        assert '依赖未构建' in result.failures['workflow:parent']
        assert not result.workflows
        return
    assert not result.failures
    assert [flow.name for flow in result.workflows] == ['parent']
    assert result.workflows[0].dependsOn == []
    assert result.workflows[0].definition == parent['workflow']  # Host never rewrites the Agent graph.
    assert order.count('WorkflowDraft') == 2  # Child and parent each generated once.
    stage = await worker._stage('workflow:parent')
    assert stage['output']['reusedDependencies'] == parent['reusedDependencies']
    assert stage['buildInputs']['fragment']['dependsOn'] == ['redundant-child']  # Keep original plan.


@pytest.mark.parametrize('wrong', ['unknown-version', 'wrong-digest', 'unused-selection', 'missing-selection'])
def test_reuse_requires_real_published_lock_and_actual_graph_call(wrong):
    from app.application.learning_agents import WorkflowDraft, bind_workflow_schema
    from app.application.dependency_reuse_contract import validate_dependency_reuse
    from jsonschema import Draft202012Validator
    value = responses()['WorkflowDraft']
    value['workflow']['spec']['nodes'][1].update(type='capability_call', config={
        'capabilityVersionId': 'published', 'interface': 'execute', 'digest': 'sha256:published'})
    value['reusedDependencies'] = {'child': {'versionId': 'published', 'interface': 'execute',
        'digest': 'sha256:published', 'reason': 'Reuse existing stage'}}
    data = {'skippedDependencies': [{'name': 'child'}], 'catalog': {'capabilities': [
        {'id': 'published', 'digest': 'sha256:published', 'interfaces': {'execute': {}}}]}}
    if wrong == 'unknown-version': value['reusedDependencies']['child']['versionId'] = 'missing'
    if wrong == 'wrong-digest': value['reusedDependencies']['child']['digest'] = 'sha256:wrong'
    if wrong == 'unused-selection': value['workflow']['spec']['nodes'][1]['config']['capabilityVersionId'] = 'other'
    if wrong == 'missing-selection': value['reusedDependencies'] = {}
    schema = bind_workflow_schema(WorkflowDraft.model_json_schema(), data)
    assert list(Draft202012Validator(schema).iter_errors(value))
    with pytest.raises(ValueError, match='复用选择'):
        validate_dependency_reuse(WorkflowDraft.model_validate(value), data)


@pytest.mark.parametrize("repair_succeeds", [True, False])
async def test_output_contract_repair_is_bounded_and_accounts_both_calls(
    monkeypatch, repair_succeeds,
):
    from app.application.agent_host import AgentOutputValidationError

    worker = await pipeline()
    requests = []

    async def execute(self, request, *, event_sink, **kwargs):
        requests.append(request)
        await event_sink({}, {"type": "agent.step.completed", "payload": {
            "requestId": "repair-" + str(len(requests)), "inputTokens": 10,
            "outputTokens": 5, "usageComplete": True,
        }})
        if len(requests) == 2:
            assert "validationFeedback" in request.prompt
            assert "required" in request.prompt
            if repair_succeeds:
                return AgentHostResult(
                    output={"decision": "skip", "reason": "无新增能力"}, total_tokens=15)
        raise AgentOutputValidationError(
            "CONTRACT_VIOLATION", "required decision", candidate={})

    monkeypatch.setattr(AgentHost, "execute", execute)
    if repair_succeeds:
        result = await worker.call("extractor", ExtractionPlan, {"calls": []})
        assert result.decision == "skip"
    else:
        with pytest.raises(AgentOutputValidationError):
            await worker.call("extractor", ExtractionPlan, {"calls": []})
    assert len(requests) == 2
    stage = await worker._stage("extractor")
    assert stage["formatRepairCount"] == 1
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        assert sum(job.result_json["usageByRequest"].values()) == 30
