import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.application.dsh_extraction import DshExtractionCreate, external_extraction_calls, start_dsh_extraction
from app.infrastructure.database import SessionLocal
from app.infrastructure.models.tasks import Invocation, LearningJob, Task


def snapshot(**updates):
    return DshExtractionCreate(**({"sessionId": "session-1", "messages": [{"role": "user", "content": "Count words"}],
        "toolEvidence": [{"id": "call-1", "name": "python", "status": "succeeded", "input": {"text": "hello"},
                          "output": {"count": 1}}]} | updates))


async def test_manual_extraction_queues_real_learning_without_fabricated_invocations():
    async with SessionLocal() as session:
        result = await start_dsh_extraction(session, snapshot())
        task = await session.get(Task, result["taskId"])
        job = await session.get(LearningJob, result["learningJobId"])
        assert task.status == "learning"
        assert task.state_json["extractionRequirements"]["requireWorkflow"] is True
        assert job.kind == "distill" and not job.result_json.get("automatic")
        assert list(await session.scalars(select(Invocation))) == []
        source = task.state_json["extractionSource"]
        assert source["invocationIds"] == []
        calls = external_extraction_calls(source)
        assert calls[0].id.startswith("dsh:")
        assert calls[0].provenance["verifiedByHost"] is False
        assert calls[0].output_json == {"count": 1}
        assert task.runtime_json


def test_external_evidence_can_ground_examples_but_must_match_observed_output():
    from app.application.learning_contracts import ContractExample
    from app.application.learning_evidence import validate_examples
    source = {"provider": "dsh", "sessionId": "session-1", "externalEvidence": {
        "provider": "dsh", "sessionId": "session-1", "tools": [item.model_dump() for item in snapshot().toolEvidence]}}
    calls = external_extraction_calls(source)
    evidence = {call.id: call for call in calls}
    examples = [ContractExample(input={"text": "hello"}, expected={"count": 1}, sourceInvocationId=calls[0].id),
                ContractExample(input={"text": "two words"}, expected={"count": 2})]
    validate_examples(examples, evidence)
    examples[0].expected = {"count": 99}
    with pytest.raises(ValueError, match="样例输出"):
        validate_examples(examples, evidence)


def test_snapshot_bounds_and_evidence_identity():
    with pytest.raises(ValidationError):
        snapshot(messages=[{"role": "user", "content": "x" * 65537}])
    with pytest.raises(ValidationError):
        snapshot(messages=[{"role": "user", "content": "字" * 60000}] * 6)
    with pytest.raises(ValidationError):
        snapshot(toolEvidence=[item.model_dump() for item in snapshot().toolEvidence] * 2)
    with pytest.raises(ValueError, match="identity"):
        external_extraction_calls({"provider": "dsh", "sessionId": "one", "externalEvidence": {"provider": "dsh", "sessionId": "two"}})


async def test_worker_publishes_from_external_evidence_and_finishes_conversation(monkeypatch, tmp_path):
    from app.application.task_learning import LearningDriver
    from app.config import get_settings
    from tests.test_learning_fragments import mock_proposal
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path / "capabilities")
    async with SessionLocal() as session:
        result = await start_dsh_extraction(session, snapshot(requireWorkflow=False))
        task = await session.get(Task, result["taskId"])
        call_id = external_extraction_calls(task.state_json["extractionSource"])[0].id
    mock_proposal(monkeypatch, {"decision": "candidate", "reason": "Reusable method",
        "skills": [{"name": "dsh-count-method", "displayName": "Count method",
                    "description": "Count words from supplied text", "content": "Split the supplied text and count the resulting words.",
                    "useWhen": ["Count words"], "sourceInvocationIds": [call_id]}]})
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, result["learningJobId"])
        task = await session.get(Task, result["taskId"])
        assert job.status == "published", job.result_json
        assert task.status == "succeeded"
        assert list(await session.scalars(select(Invocation))) == []
        assert len(list(await session.scalars(select(LearningJob)))) == 1


async def test_external_snapshot_produces_verified_workflow(monkeypatch, tmp_path):
    from app.application.task_learning import LearningDriver
    from app.config import get_settings
    from app.infrastructure.database import WorkflowVersion
    from tests.fixtures import deterministic_workflow
    from tests.test_learning_fragments import mock_proposal
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path / "capabilities")
    body = snapshot(toolEvidence=[{"id": "greeting", "name": "python", "status": "succeeded",
                                  "output": {"message": "你好，Alice"}}])
    async with SessionLocal() as session:
        result = await start_dsh_extraction(session, body)
        task = await session.get(Task, result["taskId"])
        call_id = external_extraction_calls(task.state_json["extractionSource"])[0].id
    definition = deterministic_workflow()
    definition["metadata"]["capability"] = {"summary": "格式化问候", "useWhen": ["生成问候"],
        "doNotUseWhen": ["分析任务"], "examples": ["问候 Alice"], "tags": ["文本"]}
    mock_proposal(monkeypatch, {"decision": "candidate", "reason": "Reusable greeting",
        "workflow": definition, "workflowScope": "固定问候", "workflowSourceInvocationIds": [call_id],
        "workflowExamples": [{"input": {"name": "Alice"}, "expected": {"message": "你好，Alice"},
                              "sourceInvocationId": call_id},
                             {"input": {"name": "Bob"}, "expected": {"message": "你好，Bob"}}]})
    assert await LearningDriver().poll_once()
    async with SessionLocal() as session:
        job = await session.get(LearningJob, result["learningJobId"])
        assert job.status == "published", job.result_json
        version = await session.get(WorkflowVersion, job.result_json["workflows"]["fixed-workflow"])
        assert version.definition_json["metadata"]["sourceInvocationIds"] == [call_id]
        assert list(await session.scalars(select(Invocation))) == []
