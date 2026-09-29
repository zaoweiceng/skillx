import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.application.invocations import InvocationDriver, enqueue_invocation
from app.application.local_execution import task_directory
from app.config import get_settings
from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.tasks import Invocation, Task, TaskEvent
from app.infrastructure.payloads import MARKER


async def test_large_payload_round_trip_digest_and_reference_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    task_id = str(uuid4())
    payload = {
        "text": "large" * 20000,
        "versionId": "stable",
        "untrusted": {MARKER: {"sha256": "a" * 64, "size": 1}},
    }
    async with SessionLocal() as session, session.begin():
        session.add(Task(id=task_id, goal="audit", status="queued"))
        await session.flush()
        session.add(TaskEvent(task_id=task_id, type="audit", payload_json=payload))
    async with SessionLocal() as session:
        event = await session.scalar(select(TaskEvent))
        assert event.payload_json == payload
        raw = json.loads(
            (await session.execute(text("SELECT payload_json FROM task_events"))).scalar()
        )
        assert raw["versionId"] == "stable"
        reference = raw["text"][MARKER]
        path = tmp_path / "payloads" / (reference["sha256"] + ".json")
        assert path.exists()
        path.write_text("tampered")
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="校验失败"):
            await session.scalar(select(TaskEvent))


async def test_worker_reattaches_command_without_repeating_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    task_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        task = Task(id=task_id, goal="durable code", status="waiting_execution", runtime_json={})
        session.add(task)
        await session.flush()
        item = await enqueue_invocation(
            session,
            task=task,
            key="once",
            kind="code",
            arguments={
                "language": "python",
                "source": "from pathlib import Path\nimport time\n"
                "with Path('counter').open('a') as stream: stream.write('x')\n"
                "time.sleep(2)\nprint('done')\n",
            },
        )
        invocation_id = item.id
    original = asyncio.create_task(InvocationDriver().poll_once())
    counter = task_directory(task_id) / "counter"
    for _ in range(100):
        if counter.exists():
            break
        await asyncio.sleep(0.1)
    assert counter.exists()
    original.cancel()  # Worker lifetime ends; the detached command remains owned.
    await asyncio.gather(original, return_exceptions=True)
    async with SessionLocal() as session, session.begin():
        item = await session.get(Invocation, invocation_id)
        item.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        item = await session.get(Invocation, invocation_id)
        assert item.status == "succeeded", item.error_json
        assert item.output_json["stdout"] == "done\n"
    assert counter.read_text() == "x"


async def test_payload_download_is_task_scoped(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api.tasks import download_task_artifact, task_artifacts

    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    first, other = str(uuid4()), str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add_all([Task(id=first, goal="owner"), Task(id=other, goal="other")])
        await session.flush()
        session.add(TaskEvent(task_id=first, type="audit", payload_json={"text": "a" * 80000}))
    async with SessionLocal() as session:
        artifacts = await task_artifacts(first, session)
        path = artifacts[0]["path"]
        response = await download_task_artifact(first, path, session)
        assert response.media_type == "application/json"
        with pytest.raises(HTTPException) as exc:
            await download_task_artifact(other, path, session)
        assert exc.value.status_code == 404


async def test_generic_recovery_preserves_completed_effect_and_validates_contract():
    from app.application.orchestrator import Orchestrator
    from app.application.recovery import RecoveryError, apply_recovery_decision, verify_checkpoint
    from app.domain.workflow import WorkflowDefinition
    from app.infrastructure.database import ExecutionCheckpoint, NodeRun, Run
    from tests.test_recovery import OUTPUT_SCHEMA, _recovery_workflow, _seed_failed_run

    run_id, old_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        await session.delete(await session.get(ExecutionCheckpoint, old_id))
        await session.flush()
        run = await session.get(Run, run_id)
        run.runtime_snapshot_json = {"automaticRecovery": {"agentRuntimeSnapshot": {}}}
        raw = _recovery_workflow()
        risky = next(n for n in raw["spec"]["nodes"] if n["id"] == "risky")
        risky.pop("fallback")
        risky["config"]["outputSchema"] = OUTPUT_SCHEMA
        definition = WorkflowDefinition.model_validate(raw)
        failed = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
        )
        await Orchestrator()._handle_fatal(session, run, definition, failed)
        checkpoint = await session.scalar(select(ExecutionCheckpoint))
        verify_checkpoint(checkpoint)
        assert run.status == "recovering"
        assert checkpoint.snapshot_json["committedSideEffects"][0]["nodeId"] == "sensitive"
        assert "target" not in checkpoint.snapshot_json["fallbackConfig"]
        with pytest.raises(RecoveryError):
            await apply_recovery_decision(
                session,
                checkpoint,
                {"action": "provide_substitution", "substitution": {"value": 123}},
            )
        await apply_recovery_decision(
            session,
            checkpoint,
            {"action": "provide_substitution", "substitution": {"value": "recovered"}},
        )
        assert failed.status == "failed"
        assert (
            len(
                list(
                    await session.scalars(
                        select(NodeRun).where(
                            NodeRun.run_id == run_id, NodeRun.node_id == "sensitive"
                        )
                    )
                )
            )
            == 1
        )


async def test_recovery_question_card_answer_is_atomic_and_idempotent():
    from app.application.human_projection import sync_workflow_interactions
    from app.application.recovery import apply_recovery_decision
    from app.application.tasks import answer_human
    from app.infrastructure.database import Approval, ExecutionCheckpoint, Run
    from app.infrastructure.models.tasks import HumanInteraction
    from tests.test_recovery import _seed_failed_run

    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(
            session,
            checkpoint,
            {"action": "request_user", "question": "请输入真实结果", "reason": "缺少外部证据"},
        )
        await sync_workflow_interactions(session)
        card = await session.scalar(select(HumanInteraction))
        card_id, digest = card.id, card.snapshot_digest
        assert card.kind == "information"
    async with SessionLocal() as session, session.begin():
        for _ in range(2):
            answered = await answer_human(
                session,
                card_id,
                revision=1,
                snapshot_digest=digest,
                answer={"answer": "已核实"},
                key="same",
            )
            assert answered.status == "answered"
        assert (await session.get(Run, run_id)).status == "recovering"
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        assert checkpoint.snapshot_json["userAnswer"] == "已核实"
        assert (await session.scalar(select(Approval))).status == "approved"
    async with SessionLocal() as session, session.begin():
        with pytest.raises(ValueError, match="已被回答"):
            await answer_human(
                session,
                card_id,
                revision=1,
                snapshot_digest=digest,
                answer={"answer": "不同"},
                key="other",
            )


def test_active_budget_counts_parallel_union_and_excludes_waits():
    from app.application.task_budget import union_milliseconds

    assert union_milliseconds([(0, 10), (5, 20), (100, 110)]) == 30000
    assert union_milliseconds([(0, 10), (0, 10), (1, 5)]) == 10000


async def test_invalid_recovery_decision_finishes_instead_of_retrying_forever(monkeypatch):
    from app.application.compiler import digest_json
    from app.application.recovery import RecoveryDriver
    from app.infrastructure.database import ExecutionCheckpoint, Run
    from tests.test_recovery import OUTPUT_SCHEMA, _seed_failed_run

    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        checkpoint.snapshot_json = {
            **checkpoint.snapshot_json,
            "failedNode": {**checkpoint.snapshot_json["failedNode"], "outputSchema": OUTPUT_SCHEMA},
        }
        checkpoint.digest = digest_json(checkpoint.snapshot_json)

    async def invalid(*args, **kwargs):
        return {"action": "provide_substitution", "substitution": {"wrong": True}}

    monkeypatch.setattr("app.application.recovery.run_recovery_agent", invalid)
    driver = RecoveryDriver()
    assert await driver.poll_once()
    assert not await driver.poll_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_json["code"] == "DEPENDENCY_UNAVAILABLE"
        assert run.error_json["recoveryError"]["code"] == "CONTRACT_VIOLATION"
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        assert checkpoint.status == "failed"
        assert checkpoint.decision_json["rejected"]


async def test_model_skill_business_assertions_gate_materialization(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.application.task_learning import SkillProposal, materialize_skill
    from app.domain.workflow import NodeResult

    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)

    async def runtime(session, kind):
        return {"agentConfig": {"provider": "test-adapter"}}

    async def execute(version, interface, inputs, *args, **kwargs):
        return NodeResult(status="succeeded", output={"category": inputs["text"]})

    monkeypatch.setattr("app.application.model_materialization.model_runtime", runtime)
    monkeypatch.setattr("app.application.model_materialization.execute_skillx", execute)
    proposal = SkillProposal(
        name="classify",
        description="分类",
        kind="agent",
        prompt="分类输入文本",
        inputSchema={"type": "object"},
        outputSchema={"type": "object"},
        examples=[
            {
                "input": {"text": "a"},
                "expected": {"category": "a"},
                "sourceInvocationId": "original",
            },
            {"input": {"text": "b"}, "expected": {"category": "b"}},
        ],
    )
    evidence = {
        "original": SimpleNamespace(
            status="succeeded", kind="skillx", output_json={"category": "a"}
        )
    }
    root = await materialize_skill(proposal, "job", evidence)
    assert json.loads((root / "validation.json").read_text())["profile"] == "model"
    proposal.examples[1].expected = {"category": "incorrect"}
    with pytest.raises(ValueError, match="业务断言"):
        await materialize_skill(proposal, "bad-job", evidence)


async def test_external_write_validation_replays_exact_captured_input(monkeypatch):
    from types import SimpleNamespace

    from app.application.learning_validation import validate_observed_capability

    version = SimpleNamespace(
        id="v1",
        manifest_json={
            "interfaces": {
                "send": {
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "kind": "direct",
                    "effects": [{"type": "write", "confirmation": "required"}],
                }
            }
        },
    )
    call = SimpleNamespace(
        id="call",
        kind="skillx",
        status="succeeded",
        request_json={"versionId": "v1", "interface": "send", "input": {"n": 1}},
        output_json={"receipt": "saved"},
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("external write was replayed live")

    monkeypatch.setattr("app.application.skillx_execution.execute_skillx", forbidden)
    result = await validate_observed_capability(version, "send", {"n": 1}, {"call": call}, "test")
    assert result.output == {"receipt": "saved"}
    with pytest.raises(ValueError, match="原始调用证据"):
        await validate_observed_capability(version, "send", {"n": 2}, {"call": call}, "test")


async def test_generated_skill_reuses_locked_dependency_and_clean_fixtures(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from types import SimpleNamespace

    from app.application.capabilities import register_skillx
    from app.application.skillx_execution import execute_skillx
    from app.application.task_learning import SkillProposal, materialize_skill

    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)
    source = Path(__file__).resolve().parents[2] / "capabilities/skillx/text-stats"
    shutil.copytree(source, tmp_path / "text-stats")
    async with SessionLocal() as session, session.begin():
        _, dependency = await register_skillx(session, "text-stats")
    evidence = {}
    examples = []
    for sample_text in ["one", "two words"]:
        invocation_id = str(uuid4())
        result = await execute_skillx(dependency.id, "count", {"text": sample_text}, invocation_id)
        assert result.status == "succeeded"
        evidence[invocation_id] = SimpleNamespace(
            id=invocation_id,
            kind="skillx",
            status="succeeded",
            output_json=result.output,
            request_json={
                "versionId": dependency.id,
                "interface": "count",
                "input": {"text": sample_text},
            },
        )
        examples.append(
            {
                "input": {"text": sample_text},
                "expected": result.output,
                "sourceInvocationId": invocation_id,
            }
        )
    proposal = SkillProposal(
        name="observed-stats",
        description="复用文本统计",
        inputSchema={"type": "object"},
        outputSchema={"type": "object"},
        source="async def execute(request, context):\n"
        "    return await context.capabilities.call('stats', request.input)\n",
        dependencies={"stats": {"kind": "skillx", "ref": "text-stats@1.0.0#count"}},
        examples=examples,
    )
    root = await materialize_skill(proposal, "dependency-job", evidence)
    proof = json.loads((root / "dependency-validation.json").read_text())
    assert proof["locks"]["stats"]["versionId"] == dependency.id
    assert len(proof["fixtures"]) == 2
    async with SessionLocal() as session, session.begin():
        _, generated = await register_skillx(session, str(root.relative_to(tmp_path)))
    result = await execute_skillx(
        generated.id, "execute", {"text": "new input works"}, str(uuid4())
    )
    expected = await execute_skillx(
        dependency.id, "count", {"text": "new input works"}, str(uuid4())
    )
    assert result.status == "succeeded", result.error
    assert result.output == expected.output


async def test_automatic_repair_cannot_add_model_or_effect_grants():
    from app.application.learning_permissions import dependency_scope, verify_repair_authorization

    original = {"spec": {"nodes": [{"type": "transform"}]}}
    with pytest.raises(ValueError, match="扩大权限"):
        await verify_repair_authorization(None, original, {"spec": {"nodes": [{"type": "agent"}]}})
    with pytest.raises(ValueError, match="扩大权限"):
        await verify_repair_authorization(
            None, original, {"spec": {"nodes": [{"type": "transform", "sideEffect": True}]}}
        )
    assert dependency_scope({"kind": "skillx", "ref": "stats@1.0.1#count"}) == dependency_scope(
        {"kind": "skillx", "ref": "stats@1.0.0#count"}
    )


async def test_distinct_faults_in_one_task_do_not_suppress_each_other():
    from app.infrastructure.models.tasks import LearningJob

    async with SessionLocal() as session, session.begin():
        task = Task(id=str(uuid4()), goal="two failures")
        session.add(task)
        await session.flush()
        session.add_all(
            [
                LearningJob(id=str(uuid4()), task_id=task.id, kind="repair", fingerprint=key)
                for key in ["version-a:fault-a", "version-b:fault-b"]
            ]
        )
    async with SessionLocal() as session:
        assert len(list(await session.scalars(select(LearningJob)))) == 2


async def test_waiting_branch_does_not_block_independent_work_or_expire_budget():
    from app.api.schemas import Decision
    from app.application.approval_transitions import approve
    from app.application.orchestrator import Orchestrator, WorkerService
    from app.infrastructure.database import Approval, NodeRun, Run
    from tests.test_worker_correctness import create_run, diamond_workflow

    definition = diamond_workflow()
    definition["spec"]["nodes"][1]["type"] = "approval"
    definition["spec"]["nodes"][-1]["input"] = {"result": "${right.output.value}"}
    run_id = await create_run(definition)
    worker = WorkerService("branch-wait")
    await worker.run_once()  # input
    await worker.run_once()  # approval (the other branch remains runnable)
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "running"
    await worker.run_once()  # independent transform
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        assert run.status == "waiting_approval"
        right = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "right")
        )
        assert right.status == "succeeded"
        run.started_at = utcnow() - timedelta(hours=1)
        run.runtime_snapshot_json = {
            **run.runtime_snapshot_json,
            "waitingSince": (utcnow() - timedelta(hours=1)).isoformat(),
        }
        await Orchestrator().advance_run(session, run_id)
        assert run.status == "waiting_approval"
        assert run.cancellation_requested_at is None
        approval = await session.scalar(select(Approval).where(Approval.run_id == run_id))
        await approve(approval.id, Decision(snapshotDigest=approval.snapshot_digest), session)
    await worker.run_once()
    async with SessionLocal() as session:
        assert (await session.get(Run, run_id)).status == "succeeded"


async def test_effectful_branches_cannot_claim_the_same_resource_concurrently():
    from app.application.orchestrator import WorkerService
    from tests.test_worker_correctness import create_run, diamond_workflow

    definition = diamond_workflow(max_parallel=4)
    definition["spec"]["nodes"][1]["sideEffect"] = True
    definition["spec"]["nodes"][2]["sideEffect"] = True
    await create_run(definition)
    worker = WorkerService("resource-1")
    await worker.run_once()
    async with SessionLocal() as session, session.begin():
        assert await worker.claim(session) is not None
    async with SessionLocal() as session, session.begin():
        assert await WorkerService("resource-2").claim(session) is None
