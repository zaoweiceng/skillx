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
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
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
            "workflow": {
                "metadata": {},
                "spec": {"nodes": [{"config": {"generatedSkill": "calculate"}}]},
            },
            "workflowExamples": [
                {"input": {"value": 1}, "expected": {"value": 1}, "sourceInvocationId": "flow-call"}
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
    assert proposal.workflowScope == "可重复利用的独立片段"
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
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    failed = True

    async def verify(skill, ids):
        if failed:
            raise ValueError("执行结果与业务断言不一致")
        return await materializer(order)(skill, ids)

    with pytest.raises(ValueError, match="业务断言"):
        await worker.generate(context(), materializer=verify)
    failed = False
    await worker.generate(context(), materializer=verify)
    assert order == ["ExtractionPlan", "SkillXDraft", "SkillXDraft", "validate:calculate"]
    assert data(requests[-1])["validationFeedback"]["error"] == "执行结果与业务断言不一致"


async def test_rejected_dependency_does_not_dispatch_workflow_builder(monkeypatch):
    worker = await pipeline()
    values = responses()
    values["ExtractionPlan"]["skills"] = []
    values["SkillXDraft"] = {"decision": "skip", "reason": "没有可验证的规则", "skillx": None}
    requests, order = [], []
    mock_agents(monkeypatch, values, requests, order)
    proposal = await worker.generate(context(), materializer=materializer(order))
    assert proposal.decision == "skip"
    assert "工作流依赖未构建" in proposal.reason
    assert order == ["ExtractionPlan", "SkillXDraft"]


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
    with pytest.raises(ValueError, match="范围内"):
        await worker.generate(context(), materializer=materializer(order))
    assert order == ["ExtractionPlan", "SkillDraft"]


def test_extractor_cannot_route_unknown_dependencies_or_fabricated_calls():
    value = responses()["ExtractionPlan"]
    value["workflow"]["dependsOn"] = ["unknown"]
    with pytest.raises(ValueError, match="依赖"):
        validate_extraction(ExtractionPlan.model_validate(value), context())
    value["workflow"]["dependsOn"] = ["calculate"]
    value["skills"][0]["sourceInvocationIds"] = ["invented"]
    with pytest.raises(ValueError, match="执行证据"):
        validate_extraction(ExtractionPlan.model_validate(value), context())
