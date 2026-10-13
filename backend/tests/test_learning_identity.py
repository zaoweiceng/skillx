from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.invocations import InvocationDriver, enqueue_invocation
from app.application.task_contracts import TaskCall
from app.application.task_learning import (
    ContractExample,
    LearningProposal,
    SkillProposal,
)
from app.application.tasks import finish_task
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import Invocation, LearningJob, Task


def test_standalone_skillx_is_not_automatically_wrapped_as_workflow():
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    skill = SkillProposal(
        name="identity-test",
        description="整数传递" * 50,
        inputSchema=schema,
        outputSchema=schema,
        source="def execute(request, context): return request.input",
        examples=[
            ContractExample(
                input={"value": 1}, expected={"value": 1}, sourceInvocationId="witness"
            ),
            ContractExample(input={"value": 2}, expected={"value": 2}),
        ],
    )
    proposal = LearningProposal(decision="candidate", reason="repeat", skillx=[skill])
    assert proposal.workflow is None
    assert proposal.workflowExamples == []
    assert proposal.skillx[0].examples[0].sourceInvocationId == "witness"
    assert proposal.skills == []


@pytest.mark.asyncio
async def test_model_call_persists_real_output_and_enters_learning(monkeypatch):
    async def execute(self, req, **kwargs):
        assert req.requested_capabilities == ()
        assert "消息" in req.prompt
        return AgentHostResult(output={"labels": ["other"]}, total_tokens=10)

    monkeypatch.setattr(AgentHost, "execute", execute)
    monkeypatch.setattr(
        "app.application.runtime_profiles.restore_agent_snapshot", lambda value: SimpleNamespace()
    )
    tid = str(uuid4())
    async with SessionLocal() as session, session.begin():
        task = Task(
            id=tid,
            goal="语义分类",
            status="waiting_execution",
            state_json={},
            runtime_json={},
            output_json={},
        )
        session.add(task)
        await session.flush()
        call = TaskCall(
            key="model",
            kind="model",
            reason="semantic",
            arguments={
                "prompt": "分类消息",
                "input": {"messages": ["谢谢"]},
                "outputSchema": {
                    "type": "object",
                    "properties": {"labels": {"type": "array", "items": {"enum": ["other"]}}},
                    "required": ["labels"],
                },
            },
        )
        inv = await enqueue_invocation(
            session, task=task, kind=call.kind, arguments=call.arguments, key=call.key
        )
        iid = inv.id
    await InvocationDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        inv = await session.get(Invocation, iid)
        assert inv.status == "succeeded"
        assert inv.output_json == {"labels": ["other"]}
        task = await session.get(Task, tid)
        task.state_json = {"learningCandidate": {
            "kind": "new_method", "novelty": "新增语义分类规则",
            "verification": "已验证分类结果", "sourceInvocationIds": [iid],
        }}
        await finish_task(session, task, "succeeded", "完成")
    async with SessionLocal() as session:
        assert await session.scalar(select(LearningJob.id).where(LearningJob.task_id == tid))
