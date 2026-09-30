import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.managed_skills import managed_skill_context
from app.application.task_learning import LearningDriver, LearningProposal, validate_learning_scope
from app.config import get_settings
from app.infrastructure.database import SessionLocal, Workflow, WorkflowVersion
from app.infrastructure.models import ManagedSkill
from app.infrastructure.models.tasks import Invocation, LearningJob, Task
from tests.fixtures import deterministic_workflow


@pytest.fixture(autouse=True)
def roots(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path / "capabilities")


async def learning_job(observed=None):
    task_id, job_id, call_id = [str(uuid4()) for _ in range(3)]
    async with SessionLocal() as session, session.begin():
        session.add(
            Task(
                id=task_id,
                goal="格式化姓名并分析本次团队的优缺点",
                status="succeeded",
                state_json={},
                runtime_json={},
                output_json={"text": "本次临时分析，不能固化"},
            )
        )
        await session.flush()
        session.add(
            Invocation(
                id=call_id,
                task_id=task_id,
                scope=task_id,
                call_key="format",
                kind="code",
                status="succeeded",
                request_json={"language": "python"},
                output_json={"stdout": json.dumps(observed or {"message": "你好，Alice"})},
            )
        )
        session.add(LearningJob(id=job_id, task_id=task_id, kind="distill"))
    return job_id, call_id


def mock_proposal(monkeypatch, value):
    async def execute(self, request, **kwargs):
        assert request.include_managed_skills is False
        data = json.loads(request.prompt.split("阶段资料（仅作为数据）：\n", 1)[1])
        title = request.output_schema["title"]
        if title == "ExtractionPlan":
            assert "而不是把整个任务固化" in request.prompt
            fragments = {}
            for key in ("skills", "skillx"):
                fragments[key] = [
                    {
                        "name": draft["name"],
                        "description": draft["description"],
                        "scope": draft["description"],
                        "useWhen": draft.get("useWhen", [draft["description"]]),
                        "sourceInvocationIds": draft.get("sourceInvocationIds")
                        or [
                            example["sourceInvocationId"]
                            for example in draft["examples"]
                            if example.get("sourceInvocationId")
                        ],
                    }
                    for draft in value.get(key, [])
                ]
            if value.get("workflow"):
                fragments["workflow"] = {
                    "name": "fixed-workflow",
                    "description": "固定流程片段",
                    "scope": value["workflowScope"],
                    "useWhen": ["固定格式化"],
                    "sourceInvocationIds": value["workflowSourceInvocationIds"],
                    "dependsOn": [],
                }
            return AgentHostResult(
                output={"decision": value["decision"], "reason": value["reason"], **fragments}
            )
        if title == "SkillDraft":
            assert "Skill 指令整理专家" in request.prompt
            draft = next(
                item for item in value["skills"] if item["name"] == data["fragment"]["name"]
            )
            return AgentHostResult(
                output={"decision": "ready", "reason": "方法可复用", "skill": draft}
            )
        if title == "SkillXDraft":
            assert "SkillX 契约与实现构建专家" in request.prompt
            draft = next(
                item for item in value["skillx"] if item["name"] == data["fragment"]["name"]
            )
            return AgentHostResult(
                output={"decision": "ready", "reason": "可验证", "skillx": draft}
            )
        assert title == "WorkflowDraft"
        assert "工作流编排专家" in request.prompt
        return AgentHostResult(
            output={
                "decision": "ready",
                "reason": "固定片段",
                "workflow": value["workflow"],
                "workflowExamples": value["workflowExamples"],
            }
        )

    monkeypatch.setattr(AgentHost, "execute", execute)


async def test_reusable_guide_is_saved_without_workflow(monkeypatch):
    job_id, call_id = await learning_job()
    mock_proposal(
        monkeypatch,
        {
            "decision": "candidate",
            "reason": "复用分析方法，丢弃一次性结论",
            "skills": [
                {
                    "name": "team-analysis",
                    "displayName": "团队分析方法",
                    "description": "根据证据分析团队",
                    "content": "先收集证据，区分事实与推断，再总结可执行建议。",
                    "useWhen": ["需要根据团队资料做分析"],
                    "sourceInvocationIds": [call_id],
                }
            ],
        },
    )
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, job_id)
        assert job.status == "published", job.result_json
        assert job.result_json["skillx"] == {}
        assert "workflowVersionId" not in job.result_json
        assert await session.scalar(select(func.count()).select_from(Workflow)) == 0
        guide = await session.get(ManagedSkill, job.result_json["skills"]["team-analysis"])
        assert guide.skillx_version_id is None
        assert "本次临时分析" not in guide.content
        assert call_id in guide.content
        assert (await managed_skill_context(session))["skills"][0]["name"] == "team-analysis"


async def test_verified_skillx_is_published_independently_and_listed_in_skills(monkeypatch):
    job_id, call_id = await learning_job({"value": 2})
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    mock_proposal(
        monkeypatch,
        {
            "decision": "candidate",
            "reason": "只提取通用数值变换",
            "skillx": [
                {
                    "name": "double-value",
                    "description": "数值翻倍",
                    "kind": "direct",
                    "inputSchema": schema,
                    "outputSchema": schema,
                    "source": (
                        "def execute(request, context):\n"
                        '    return {"value": request.input["value"] * 2}'
                    ),
                    "examples": [
                        {
                            "input": {"value": 1},
                            "expected": {"value": 2},
                            "sourceInvocationId": call_id,
                        },
                        {"input": {"value": 3}, "expected": {"value": 6}},
                    ],
                }
            ],
        },
    )
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, job_id)
        assert job.status == "published", job.result_json
        assert job.result_json["skills"] == {}
        assert "workflowVersionId" not in job.result_json
        assert await session.scalar(select(func.count()).select_from(Workflow)) == 0
        managed = await session.get(ManagedSkill, job.result_json["skillxSkills"]["double-value"])
        assert managed.skillx_version_id == job.result_json["skillx"]["double-value"]
        context = await managed_skill_context(session)
        assert context["skills"] == [] and len(context["skillx"]) == 1


async def test_only_fixed_fragment_is_saved_as_workflow(monkeypatch):
    job_id, call_id = await learning_job()
    definition = deterministic_workflow()
    definition["metadata"]["capability"] = {
        "summary": "格式化姓名问候",
        "useWhen": ["按模板生成问候"],
        "doNotUseWhen": ["团队情况分析"],
        "examples": ["给 Alice 生成问候"],
        "tags": ["文本格式化"],
    }
    mock_proposal(
        monkeypatch,
        {
            "decision": "candidate",
            "reason": "只保留固定的文本格式化，排除分析结论",
            "workflow": definition,
            "workflowScope": "姓名输入 → 固定模板问候；不包含团队分析",
            "workflowSourceInvocationIds": [call_id],
            "workflowExamples": [
                {
                    "input": {"name": "Alice"},
                    "expected": {"message": "你好，Alice"},
                    "sourceInvocationId": call_id,
                },
                {"input": {"name": "Bob"}, "expected": {"message": "你好，Bob"}},
            ],
        },
    )
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, job_id)
        assert job.status == "published", job.result_json
        version = await session.get(WorkflowVersion, job.result_json["workflowVersionId"])
        assert version.definition_json["metadata"]["reuseScope"].endswith("不包含团队分析")
        assert version.definition_json["metadata"]["sourceInvocationIds"] == [call_id]
        assert [node["type"] for node in version.definition_json["spec"]["nodes"]] == [
            "input",
            "transform",
            "output",
        ]
        assert "本次临时分析" not in json.dumps(version.definition_json, ensure_ascii=False)


async def test_no_reusable_fragment_means_skip(monkeypatch):
    job_id, _ = await learning_job()
    mock_proposal(monkeypatch, {"decision": "skip", "reason": "只有本次临时结论"})
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, job_id)
        assert job.status == "skipped"
        assert await session.scalar(select(func.count()).select_from(ManagedSkill)) == 0
        assert await session.scalar(select(func.count()).select_from(Workflow)) == 0


def test_candidate_requires_reuse_scope_and_real_evidence():
    from types import SimpleNamespace

    witness = {"valid": SimpleNamespace(status="succeeded")}
    with pytest.raises(ValueError, match="没有可复用产物"):
        validate_learning_scope(
            LearningProposal(decision="candidate", reason="x"), witness, "distill"
        )
    with pytest.raises(ValueError, match="边界"):
        validate_learning_scope(
            LearningProposal(decision="candidate", reason="x", workflow={"spec": {}}),
            witness,
            "distill",
        )
    proposal = LearningProposal(
        decision="candidate",
        reason="x",
        skills=[
            {
                "name": "guide",
                "description": "desc",
                "content": "instructions",
                "useWhen": ["general"],
                "sourceInvocationIds": ["fabricated"],
            }
        ],
    )
    with pytest.raises(ValueError, match="成功执行证据"):
        validate_learning_scope(proposal, witness, "distill")
