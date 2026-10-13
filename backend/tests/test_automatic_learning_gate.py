from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.task_learning_gate import automatic_learning_decision
from app.application.tasks import finish_task
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import Invocation, LearningJob, Task, TaskEvent


def nomination(ids):
    return {"learningCandidate": {"kind": "new_method", "novelty": "新增边界处理方法",
            "verification": "不同输入验证结果", "sourceInvocationIds": ids}}


def test_gate_requires_verified_delta_not_call_count():
    calls = [SimpleNamespace(id=str(i), status="succeeded", kind="code") for i in range(5)]
    assert automatic_learning_decision({}, calls)[0] is None
    assert automatic_learning_decision(nomination(["missing"]), calls)[0] is None
    calls[0].status = "failed"
    assert automatic_learning_decision(nomination(["0"]), calls)[0] is None
    assert automatic_learning_decision(nomination(["1"]), calls)[0] is not None
    calls[1].kind = "skillx"
    assert automatic_learning_decision(nomination(["1"]), calls)[0] is None


@pytest.mark.parametrize("nominated", [False, True])
async def test_finish_persists_gate_and_only_queues_budgeted_nomination(nominated):
    tid, iid = str(uuid4()), str(uuid4())
    async with SessionLocal() as session, session.begin():
        task = Task(id=tid, goal="读取文件分析", status="deciding", runtime_json={},
                    state_json=nomination([iid]) if nominated else {})
        session.add(task)
        await session.flush()
        session.add(Invocation(id=iid, task_id=tid, kind="code", status="succeeded",
                               scope="task:" + tid, call_key="read", request_json={},
                               output_json={"stdout": "document"}))
        await session.flush()
        await finish_task(session, task, "succeeded", "答案")
    async with SessionLocal() as session:
        jobs = list(await session.scalars(select(LearningJob).where(LearningJob.task_id == tid)))
        event = await session.scalar(select(TaskEvent).where(
            TaskEvent.task_id == tid, TaskEvent.type == "learning.eligibility"))
        assert event.payload_json["eligible"] is nominated
        assert len(jobs) == int(nominated)
        if nominated:
            assert jobs[0].result_json["budget"]["maxTokens"] == 60000
            assert jobs[0].result_json["automatic"] is True
