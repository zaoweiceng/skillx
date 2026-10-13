import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.application.conversation_extraction import start_extraction
from app.config import ExtractionBudget, Settings, get_settings
from app.infrastructure.database import Conversation, Message, SessionLocal
from app.infrastructure.models.tasks import LearningJob, Task


@pytest.mark.parametrize("failure", [False, True])
async def test_validation_model_call_uses_remaining_budget_and_bills_failures(tmp_path, failure):
    from app.application.agent_host import AgentHostRequest, AgentHostResult
    from app.application.learning_validation_budget import validation_budget, enforce_validation_budget
    from app.infrastructure.database import utcnow
    from tests.test_learning_agents import pipeline

    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {"budget": {"maxDurationSeconds": 1200, "maxTokens": 200000,
                                     "startedAt": utcnow().isoformat()},
                           "usageByRequest": {"builder": 190000}}
    seen = []

    @enforce_validation_budget
    async def model(self, request, *, event_sink=None, cancellation=None):
        seen.append(request)
        await event_sink({}, {"type": "model.request.dispatched", "payload": {"requestId": "validation"}})
        await event_sink({}, {"type": "agent.step.completed", "payload": {
            "requestId": "validation", "inputTokens": 1000, "outputTokens": 5000, "usageComplete": True}})
        if failure:
            raise ValueError("output contract failed after model completed")
        return AgentHostResult(output={}, input_tokens=1000, output_tokens=5000,
                               total_tokens=6000, usage_complete=True)

    request = AgentHostRequest(run_id="validate", prompt="test", output_schema={},
                               working_directory=tmp_path, max_output_tokens=8192,
                               max_duration_seconds=1800)
    with validation_budget(worker.job_id,
                           lambda id, metrics: worker.record_validation_usage("flow", id, metrics)):
        if failure:
            with pytest.raises(ValueError, match="output contract"):
                await model(None, request)
        else:
            await model(None, request)
        assert seen[0].max_billable_tokens == 10000
        assert seen[0].max_total_tokens == 10000
        assert seen[0].max_duration_seconds <= 1200
        assert seen[0].max_output_tokens < 6000
        # Only 4000 tokens remain; reserve input before dispatch, even after a failure.
        with pytest.raises(ValueError, match="预算不足"):
            await model(None, request)
        assert len(seen) == 1
    assert request.max_output_tokens == 8192  # No mutation of caller policy.
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        assert job.result_json["usageByRequest"]["validation"] == 6000


async def test_validation_unknown_usage_stops_following_calls_and_scope_resets(tmp_path):
    from app.application.agent_host import AgentHostRequest
    from app.application.learning_validation_budget import validation_budget, enforce_validation_budget
    from app.infrastructure.database import utcnow
    from tests.test_learning_agents import pipeline

    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {"budget": {"maxDurationSeconds": 1200, "maxTokens": 200000,
                                     "startedAt": utcnow().isoformat()}}
    calls = []

    @enforce_validation_budget
    async def interrupted(self, request, *, event_sink=None, cancellation=None):
        calls.append(request)
        if event_sink:
            await event_sink({}, {"type": "model.request.dispatched", "payload": {"requestId": "unknown"}})
            raise ValueError("provider disconnected")
        return "ordinary call"

    request = AgentHostRequest(run_id="validate", prompt="test", output_schema={}, working_directory=tmp_path)
    with validation_budget(worker.job_id,
                           lambda id, metrics: worker.record_validation_usage("flow", id, metrics)):
        with pytest.raises(ValueError, match="disconnected"):
            await interrupted(None, request)
        with pytest.raises(ValueError, match="用量不完整"):
            await interrupted(None, request)
        assert len(calls) == 1
    assert await interrupted(None, request) == "ordinary call"


def test_source_budget_environment_is_typed_and_rejects_invalid_policy(monkeypatch):
    policy = {"source": {"maxDurationSeconds": 1200, "maxTokens": 200000,
                         "requireWorkflowComposition": True}}
    monkeypatch.setenv("SKILLX_EXTRACTION_SOURCE_BUDGETS", json.dumps(policy))
    settings = Settings(_env_file=None)
    assert settings.extraction_source_budgets["source"].model_dump() == policy["source"]
    policy["source"]["maxTokens"] = 0
    monkeypatch.setenv("SKILLX_EXTRACTION_SOURCE_BUDGETS", json.dumps(policy))
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


async def test_one_click_budget_is_source_scoped_fresh_per_job_and_overridable(monkeypatch):
    source_id, other_id = str(uuid4()), str(uuid4())
    policy = ExtractionBudget(maxDurationSeconds=1200, maxTokens=200000,
                              requireWorkflowComposition=True)
    monkeypatch.setattr(get_settings(), "extraction_source_budgets", {source_id: policy})
    async with SessionLocal() as session:
        for id in (source_id, other_id):
            session.add(Conversation(id=id, title="来源"))
            await session.flush()
            session.add(Message(id=str(uuid4()), conversation_id=id, role="user",
                                content_json={"text": "提炼"}))
        await session.commit()

        async def extract(id, override=None):
            result = await start_extraction(session, id, override)
            task = await session.get(Task, result["taskId"])
            job = await session.scalar(select(LearningJob).where(LearningJob.task_id == task.id))
            return task, job

        first_task, first_job = await extract(source_id)
        first_budget = first_job.result_json["budget"]
        assert {key: first_budget[key] for key in ExtractionBudget.model_fields} == policy.model_dump()
        assert first_task.state_json["extractionRequirements"]["requireWorkflowComposition"] is True
        first_job.result_json = {**first_job.result_json, "usageByRequest": {"old-request": 195000}}
        await session.commit()
        _, new_job = await extract(source_id)
        assert new_job.id != first_job.id
        assert new_job.result_json["budget"]["startedAt"] != first_budget["startedAt"]
        assert "usageByRequest" not in new_job.result_json
        assert new_job.result_json["budget"]["maxTokens"] == 200000

        other_task, other_job = await extract(other_id)
        assert "budget" not in other_job.result_json
        assert other_task.state_json["extractionRequirements"]["requireWorkflowComposition"] is False
        override = ExtractionBudget(maxDurationSeconds=60, maxTokens=10000)
        explicit_task, explicit_job = await extract(source_id, override)
        assert explicit_job.result_json["budget"]["maxTokens"] == 10000
        assert explicit_task.state_json["extractionRequirements"]["requireWorkflowComposition"] is False
