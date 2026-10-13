from uuid import uuid4

import pytest

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostResult,
    AgentOutputValidationError,
)
from app.application.tasks import TaskDriver
from app.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import Task


@pytest.mark.parametrize("failure", ["terminal", "schema", "evidence", "host_schema"])
async def test_rejected_decisions_consume_budget(monkeypatch, failure):
    task_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(
            Task(
                id=task_id,
                goal="读取文件",
                status="queued",
                state_json={},
                runtime_json={},
                output_json={},
            )
        )

    consumed = get_settings().agent_max_total_tokens
    requests = []

    async def execute(self, request, **kwargs):
        requests.append(request)
        if failure == "host_schema":
            raise AgentOutputValidationError(
                "CONTRACT_VIOLATION", "calls required", total_tokens=consumed
            )
        if failure == "terminal":
            raise AgentHostError("MODEL_MALFORMED_OUTPUT", "invalid", total_tokens=consumed)
        output = (
            {}
            if failure == "schema"
            else {
                "action": "complete",
                "summary": "完成",
                "completionCriteria": ["读取"],
                "answer": "已读取",
                "evidence": [str(uuid4())],
            }
        )
        return AgentHostResult(output=output, total_tokens=consumed)

    monkeypatch.setattr(AgentHost, "execute", execute)
    driver = TaskDriver()
    assert await driver.poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.total_tokens == consumed
        assert task.status == "queued"
    assert await driver.poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.status == "blocked"
        assert task.total_tokens == consumed
    assert len(requests) == 1


async def test_final_format_failure_accounts_for_all_attempts(monkeypatch):
    from datetime import timedelta

    from app.application import tasks
    from app.application.task_budget import now

    task_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(
            Task(
                id=task_id,
                goal="读取文件",
                status="queued",
                state_json={},
                runtime_json={},
                output_json={},
            )
        )

    start = now()
    ticks = iter(start + timedelta(seconds=x) for x in (0, 1, 2, 3, 4, 5))
    monkeypatch.setattr(tasks, "now", lambda: next(ticks))

    async def execute(self, request, **kwargs):
        raise AgentHostError("MODEL_MALFORMED_OUTPUT", "invalid", total_tokens=10)

    monkeypatch.setattr(AgentHost, "execute", execute)
    driver = TaskDriver()
    for expected in ("queued", "queued", "failed"):
        assert await driver.poll_once()
        async with SessionLocal() as session:
            assert (await session.get(Task, task_id)).status == expected
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.total_tokens == 30
        assert task.active_ms == 3000
    assert not await driver.poll_once()


async def test_tool_contract_failures_are_not_retried_as_model_format(monkeypatch):
    task_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Task(id=task_id, goal="run", status="queued", state_json={},
                         runtime_json={}, output_json={}))

    async def execute(self, request, **kwargs):
        raise AgentHostError("CONTRACT_VIOLATION", "tool output invalid", total_tokens=10)

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.status == "failed"
        assert task.total_tokens == 10
        assert not task.state_json.get("formatRetries")


async def test_format_retry_preserves_candidate_then_clears_it_on_success(monkeypatch):
    task_id = str(uuid4())
    candidate = {'action': 'complete', 'summary': 'done',
                 'completionCriteria': 'must be an array', 'answer': 'done'}
    async with SessionLocal() as session, session.begin():
        session.add(Task(id=task_id, goal='answer', status='queued', state_json={},
                         runtime_json={}, output_json={}))
    requests = []

    async def execute(self, request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise AgentOutputValidationError('CONTRACT_VIOLATION', 'completionCriteria: array',
                                             candidate=candidate, total_tokens=10)
        assert 'rejectedDecision' in request.prompt
        assert 'must be an array' in request.prompt
        return AgentHostResult(
            output={**candidate, 'completionCriteria': ['answer']}, total_tokens=5,
        )

    monkeypatch.setattr(AgentHost, 'execute', execute)
    driver = TaskDriver()
    assert await driver.poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.state_json['rejectedDecision'] == candidate
    assert await driver.poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task.status == 'succeeded'
        assert 'rejectedDecision' not in task.state_json
        assert 'decisionError' not in task.state_json
        assert task.total_tokens == 15
