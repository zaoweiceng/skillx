"""006-A/B：可执行 ExecutionCheckpoint、原子领取、不可变恢复历史与真实上下文接力。

节点重试耗尽且命中显式 fallback → 冻结可执行 checkpoint → Recovery Agent 从剩余子图继续；
原失败 NodeRun 永不改写（新增 OutputSubstitution），已完成非幂等副作用调用次数不增加，
双 Worker 竞争只产生一次 Agent 调用，未知结果需人工确认。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import WorkerService
from app.application.recovery import (
    RecoveryDriver,
    RecoveryError,
    apply_recovery_decision,
    build_checkpoint,
    build_recovery_prompt,
    is_recoverable,
    parse_recovery_decision,
    verify_checkpoint,
)
from app.config import get_settings
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import (
    ExecutionCheckpoint,
    NodeRun,
    OutputSubstitution,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(settings, "traex_allowed_executables", [Path(sys.executable).name])
    monkeypatch.setattr(settings, "agent_host_capabilities", ["recover.task"])


def _recovery_workflow() -> dict[str, Any]:
    # input → sensitive(side effect, 非幂等) → risky(可重试失败, fallback→recovery) → output
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "recovery-demo"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"seed": {"type": "string"}}},
            "outputSchema": OUTPUT_SCHEMA,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "sensitive",
                    "type": "transform",
                    "displayName": "副作用步骤",
                    "input": {"value": "${input.seed}"},
                    "config": {"operation": "passthrough"},
                    "sideEffect": True,
                    "idempotent": False,
                },
                {
                    "id": "risky",
                    "type": "transform",
                    "displayName": "易失败步骤",
                    "input": {"value": "${sensitive.output.value}"},
                    "config": {"operation": "passthrough"},
                    "fallback": {
                        "on": ["DEPENDENCY_UNAVAILABLE"],
                        "target": "recovery_agent",
                    },
                },
                {
                    "id": "recovery_agent",
                    "type": "agent",
                    "displayName": "恢复 Agent",
                    "input": {"value": "${input.seed}"},
                    "config": {
                        "outputSchema": {"type": "object"},
                        "maxSteps": 4,
                        "allowedCapabilities": ["recover.task"],
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"value": "${risky.output.value}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "sensitive"},
                {"source": "sensitive", "target": "risky"},
                {"source": "risky", "target": "output"},
            ],
        },
    }


def test_is_recoverable_rejects_unsafe_codes() -> None:
    assert is_recoverable("DEPENDENCY_UNAVAILABLE", ["DEPENDENCY_UNAVAILABLE"]) is True
    assert is_recoverable("TIMEOUT", ["DEPENDENCY_UNAVAILABLE"]) is False
    assert is_recoverable("PERMISSION_DENIED", ["PERMISSION_DENIED"]) is False
    assert is_recoverable("UNKNOWN_OUTCOME", ["UNKNOWN_OUTCOME"]) is False


def test_parse_recovery_decision_discriminated_union() -> None:
    # provide_substitution 必须带 substitution。
    parsed = parse_recovery_decision(
        {"action": "provide_substitution", "substitution": {"value": "x"}}
    )
    assert parsed["substitution"] == {"value": "x"}
    with pytest.raises(RecoveryError):
        parse_recovery_decision({"action": "provide_substitution"})
    # 其它 action 不得带 substitution。
    with pytest.raises(RecoveryError):
        parse_recovery_decision({"action": "abort", "substitution": {"value": "x"}})
    with pytest.raises(RecoveryError):
        parse_recovery_decision({"action": "unknown"})


async def _seed_failed_run(*, seed: str = "s1") -> tuple[str, str]:
    """构造 sensitive 成功、risky 失败（可重试耗尽）的 Run，返回 (run_id, checkpoint_id)。"""

    definition = _recovery_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="rec", draft_revision=1, draft_json=definition)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="running",
            input_json={"seed": seed},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        session.add(
            NodeRun(
                id=str(uuid4()),
                run_id=run.id,
                node_id="sensitive",
                node_type="transform",
                attempt=1,
                status="succeeded",
                input_json={"value": seed},
                output_json={"value": seed},
                finished_at=utcnow(),
            )
        )
        risky = NodeRun(
            id=str(uuid4()),
            run_id=run.id,
            node_id="risky",
            node_type="transform",
            attempt=1,
            status="failed",
            input_json={"value": seed},
            error_json={"code": "DEPENDENCY_UNAVAILABLE", "message": "网络波动", "retryable": True},
            finished_at=utcnow(),
        )
        session.add(risky)
        await session.flush()
        definition_model = WorkflowDefinition.model_validate(definition)
        checkpoint = await build_checkpoint(session, run, definition_model, risky)
        run.status = "recovering"
        return run.id, checkpoint.id


async def test_checkpoint_freezes_completed_outputs_and_remaining() -> None:
    _run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session:
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        snapshot = checkpoint.snapshot_json
        # 已完成 sensitive 输出值被冻结；已提交非幂等副作用被记录并带幂等键。
        assert snapshot["completedOutputs"]["sensitive"] == {"value": "s1"}
        effects = snapshot["committedSideEffects"]
        assert effects[0]["nodeId"] == "sensitive"
        assert effects[0]["idempotent"] is False
        assert "idempotencyKey" in effects[0]
        # remaining 含节点定义与边，且排除 fallback-only recovery_agent。
        remaining_ids = {node["id"] for node in snapshot["remainingNodes"]}
        assert "risky" in remaining_ids
        assert "sensitive" not in remaining_ids
        assert "recovery_agent" not in remaining_ids
        assert snapshot["remainingEdges"]
        # 失败节点输入与完整错误被冻结。
        assert snapshot["failedNode"]["input"] == {"value": "s1"}
        assert snapshot["failedNode"]["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        verify_checkpoint(checkpoint)


async def test_recovery_prompt_contains_completed_values() -> None:
    from app.application.recovery import build_recovery_prompt

    _run_id, checkpoint_id = await _seed_failed_run(seed="RANDOM-XYZ")
    async with SessionLocal() as session:
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        prompt = build_recovery_prompt(checkpoint)
    # 真正把上下文交给 Agent：已完成值出现在 prompt 中，而不仅是节点 id。
    assert "RANDOM-XYZ" in prompt
    assert "completedOutputs" in prompt


async def test_recovery_resumes_without_replaying_side_effects(traex_stub: None) -> None:
    run_id, _ = await _seed_failed_run()
    driver = RecoveryDriver()
    assert await driver.poll_once() is True
    worker = WorkerService("recovery-worker")
    for _ in range(4):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded", run.error_json
        assert run.output_json["value"] is not None
        # sensitive（非幂等副作用）仍只有一次成功 attempt，未被重放。
        sensitive_count = await session.scalar(
            select(func.count())
            .select_from(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == "sensitive")
        )
        assert sensitive_count == 1
        # 原失败 risky NodeRun 保持 failed（不可变），恢复通过 substitution 记录替代输出。
        risky = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
        )
        assert risky.status == "failed"
        substitution = await session.scalar(
            select(OutputSubstitution).where(
                OutputSubstitution.run_id == run_id, OutputSubstitution.node_id == "risky"
            )
        )
        assert substitution is not None


async def test_recovery_output_depends_on_random_checkpoint_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复 Agent 输出必须依赖 checkpoint 中随机前序值（否则测试失败）。"""

    from app.application import recovery as recovery_module

    run_id, _checkpoint_id = await _seed_failed_run(seed="TOKEN-4711")

    async def fake_agent(checkpoint, fallback_config, *, host=None):  # noqa: ANN001, ARG001
        # 恢复决定必须复用 checkpoint 中已完成的 sensitive 值。
        completed = checkpoint.snapshot_json["completedOutputs"]["sensitive"]["value"]
        return {"action": "provide_substitution", "substitution": {"value": completed}}

    monkeypatch.setattr(recovery_module, "run_recovery_agent", fake_agent)
    driver = RecoveryDriver()
    assert await driver.poll_once() is True
    worker = WorkerService("recovery-worker")
    for _ in range(4):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded", run.error_json
        # 输出携带随机前序值，证明接力消费了 checkpoint 上下文。
        assert run.output_json["value"] == "TOKEN-4711"


async def test_concurrent_recovery_drivers_claim_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """双 Recovery Worker 竞争同一 checkpoint：只产生一个 Recovery NodeRun 与一次 Agent 调用。"""

    from app.application import recovery as recovery_module

    _run_id, _checkpoint_id = await _seed_failed_run()
    calls: list[str] = []

    async def counting_agent(checkpoint, fallback_config, *, host=None):  # noqa: ANN001, ARG001
        calls.append(checkpoint.id)
        await asyncio.sleep(0.05)
        return {"action": "provide_substitution", "substitution": {"value": "recovered"}}

    monkeypatch.setattr(recovery_module, "run_recovery_agent", counting_agent)
    driver_a = RecoveryDriver(worker_id="rec-a")
    driver_b = RecoveryDriver(worker_id="rec-b")
    await asyncio.gather(driver_a.poll_once(), driver_b.poll_once())
    # 只有一个 Worker 领取成功并调用 Agent。
    assert len(calls) == 1
    async with SessionLocal() as session:
        recovery_runs = await session.scalar(
            select(func.count())
            .select_from(NodeRun)
            .where(NodeRun.run_id == _run_id, NodeRun.node_id == "recovery_agent")
        )
        assert recovery_runs == 1


async def test_late_recovery_worker_cannot_commit_after_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 租约过期、B 重领后，A 的迟到决定不得修改任何持久状态。"""

    from app.application import recovery as recovery_module

    run_id, checkpoint_id = await _seed_failed_run()
    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def fenced_agent(checkpoint, fallback_config, *, host=None):  # noqa: ANN001, ARG001
        if checkpoint.lease_owner == "rec-a":
            a_started.set()
            await release_a.wait()
            return {"action": "provide_substitution", "substitution": {"value": "late-a"}}
        return {"action": "provide_substitution", "substitution": {"value": "winner-b"}}

    monkeypatch.setattr(recovery_module, "run_recovery_agent", fenced_agent)
    driver_a = RecoveryDriver(worker_id="rec-a")
    driver_b = RecoveryDriver(worker_id="rec-b")
    task_a = asyncio.create_task(driver_a.poll_once())
    await a_started.wait()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        checkpoint.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert await driver_b.poll_once() is True
    release_a.set()
    await task_a

    async with SessionLocal() as session:
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        substitution = await session.scalar(
            select(OutputSubstitution).where(
                OutputSubstitution.run_id == run_id,
                OutputSubstitution.node_id == "risky",
            )
        )
        recovery_runs = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == "recovery_agent")
                .order_by(NodeRun.attempt)
            )
        ).all()
        assert checkpoint.claim_generation >= 3
        assert checkpoint.decision_json["substitution"] == {"value": "winner-b"}
        assert substitution.output_json == {"value": "winner-b"}
        assert [item.status for item in recovery_runs] == ["lease_expired", "succeeded"]


async def test_recovery_request_user_transitions_to_waiting_approval() -> None:
    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(
            session,
            checkpoint,
            {"action": "request_user", "question": "请确认是否继续？"},
        )
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "waiting_approval"


async def test_recovery_prompt_summarizes_large_checkpoint_outputs() -> None:
    _run_id, checkpoint_id = await _seed_failed_run(seed="x" * 20_000)
    async with SessionLocal() as session:
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        stored = checkpoint.snapshot_json["completedOutputs"]["sensitive"]
        assert stored["summary"]["bytes"] > 16_384
        assert stored["nodeRunRef"]
        prompt = build_recovery_prompt(checkpoint)
        assert len(prompt.encode()) < 24_500
        assert "contextTruncated" not in prompt or '"contextTruncated": true' in prompt


async def test_typed_replan_executes_new_recovery_attempt() -> None:
    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(
            session,
            checkpoint,
            {
                "action": "retry_replanned",
                "planPatch": {
                    "nodePatches": [
                        {
                            "nodeId": "risky",
                            "changes": {"config": {"operation": "passthrough"}},
                        }
                    ],
                    "inputMappings": {},
                    "edgePatches": [],
                },
            },
        )
    worker = WorkerService("replan-worker")
    for _ in range(8):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        attempts = (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
                .order_by(NodeRun.attempt)
            )
        ).all()
        assert run is not None and run.status == "succeeded"
        assert [item.status for item in attempts] == ["failed", "succeeded"]


async def test_substitution_schema_mismatch_fails_closed() -> None:
    """替代输出不符合失败节点 outputSchema 时 fail closed。"""

    definition = _recovery_workflow()
    # 给 risky 节点声明一个 outputSchema，校验替代输出。
    definition["spec"]["nodes"][2]["config"]["outputSchema"] = OUTPUT_SCHEMA
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="rec", draft_revision=1, draft_json=definition)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="running",
            input_json={"seed": "s1"},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        risky = NodeRun(
            id=str(uuid4()),
            run_id=run.id,
            node_id="risky",
            node_type="transform",
            attempt=1,
            status="failed",
            input_json={"value": "s1"},
            error_json={"code": "DEPENDENCY_UNAVAILABLE", "message": "x", "retryable": True},
            finished_at=utcnow(),
        )
        session.add(risky)
        await session.flush()
        definition_model = WorkflowDefinition.model_validate(definition)
        checkpoint = await build_checkpoint(session, run, definition_model, risky)
        run.status = "recovering"
        checkpoint_id = checkpoint.id
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        with pytest.raises(RecoveryError):
            # 缺 required value 字段，类型不匹配 fail closed。
            await apply_recovery_decision(
                session,
                checkpoint,
                {"action": "provide_substitution", "substitution": {"wrong": 1}},
            )


async def test_cancel_recovering_run_marks_checkpoint_cancelled() -> None:
    run_id, checkpoint_id = await _seed_failed_run()
    from app.application.orchestrator import Orchestrator

    async with SessionLocal() as session, session.begin():
        await Orchestrator().request_cancellation(session, run_id)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        assert run.status == "cancelled"
        assert checkpoint.status == "cancelled"

    driver = RecoveryDriver()
    assert await driver.poll_once() is False
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "cancelled"


async def test_build_checkpoint_is_idempotent_per_failed_attempt() -> None:
    from app.application.orchestrator import Orchestrator

    run_id, _ = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        run.status = "running"
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        definition = WorkflowDefinition.model_validate(version.definition_json)
        failed = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
        )
        await Orchestrator()._handle_fatal(session, run, definition, failed)
    async with SessionLocal() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ExecutionCheckpoint)
            .where(ExecutionCheckpoint.run_id == run_id)
        )
        assert count == 1
        run = await session.get(Run, run_id)
        assert run.status == "failed"


async def test_reapplying_decision_after_terminal_fails_closed() -> None:
    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(session, checkpoint, {"action": "abort"})
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        assert checkpoint.status == "aborted"
    driver = RecoveryDriver()
    assert await driver.poll_once() is False
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"


async def test_tampered_checkpoint_fails_closed() -> None:
    _run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        checkpoint.snapshot_json = {**checkpoint.snapshot_json, "tampered": True}
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        with pytest.raises(RecoveryError):
            await apply_recovery_decision(session, checkpoint, {"action": "retry_replanned"})


def _approval_recovery_workflow() -> dict[str, Any]:
    definition = _recovery_workflow()
    # 危险错误码 + requireUserApproval：命中后先建 checkpoint 并等待人工审批。
    definition["spec"]["nodes"][2]["fallback"] = {
        "on": ["CONTRACT_VIOLATION"],
        "target": "recovery_agent",
        "requireUserApproval": True,
    }
    return definition


async def test_approval_gated_fallback_waits_then_recovers() -> None:
    """A-03：危险错误先 checkpoint + waiting_approval；批准后走同一 Recovery claim。"""

    from app.api.routes import Decision, approve
    from app.application.orchestrator import Orchestrator
    from app.infrastructure.database import Approval

    definition = _approval_recovery_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="rec", draft_revision=1, draft_json=definition)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="running",
            input_json={"seed": "s1"},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        session.add(
            NodeRun(
                id=str(uuid4()),
                run_id=run.id,
                node_id="sensitive",
                node_type="transform",
                attempt=1,
                status="succeeded",
                input_json={"value": "s1"},
                output_json={"value": "s1"},
                finished_at=utcnow(),
            )
        )
        risky = NodeRun(
            id=str(uuid4()),
            run_id=run.id,
            node_id="risky",
            node_type="transform",
            attempt=1,
            status="failed",
            input_json={"value": "s1"},
            error_json={"code": "CONTRACT_VIOLATION", "message": "契约违规", "retryable": False},
            finished_at=utcnow(),
        )
        session.add(risky)
        run_id = run.id

    # 触发致命处理：应先建 checkpoint 并进入 waiting_approval（不直接失败）。
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        definition_model = WorkflowDefinition.model_validate(version.definition_json)
        failed = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
        )
        await Orchestrator()._handle_fatal(session, run, definition_model, failed)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "waiting_approval"
        checkpoint = await session.scalar(
            select(ExecutionCheckpoint).where(ExecutionCheckpoint.run_id == run_id)
        )
        assert checkpoint.approval_status == "pending"
        approval = await session.scalar(
            select(Approval).where(Approval.run_id == run_id)
        )
        assert approval is not None
        approval_id = approval.id
        snapshot_digest = approval.snapshot_digest

    # 批准：checkpoint→approved 且 run→recovering。
    async with SessionLocal() as session:
        await approve(approval_id, Decision(snapshotDigest=snapshot_digest), session)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "recovering"
        checkpoint = await session.scalar(
            select(ExecutionCheckpoint).where(ExecutionCheckpoint.run_id == run_id)
        )
        assert checkpoint.approval_status == "approved"


async def test_rejected_recovery_keeps_failure_terminal() -> None:
    """拒绝恢复审批：Run 保持失败终态，checkpoint→rejected。"""

    from app.api.routes import reject
    from app.application.orchestrator import Orchestrator
    from app.infrastructure.database import Approval

    definition = _approval_recovery_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="rec", draft_revision=1, draft_json=definition)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="running",
            input_json={"seed": "s1"},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        risky = NodeRun(
            id=str(uuid4()),
            run_id=run.id,
            node_id="risky",
            node_type="transform",
            attempt=1,
            status="failed",
            input_json={"value": "s1"},
            error_json={"code": "CONTRACT_VIOLATION", "message": "契约违规", "retryable": False},
            finished_at=utcnow(),
        )
        session.add(risky)
        run_id = run.id
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        definition_model = WorkflowDefinition.model_validate(version.definition_json)
        failed = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "risky")
        )
        await Orchestrator()._handle_fatal(session, run, definition_model, failed)
    async with SessionLocal() as session:
        approval = await session.scalar(select(Approval).where(Approval.run_id == run_id))
        approval_id = approval.id
    async with SessionLocal() as session:
        await reject(approval_id, session)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        checkpoint = await session.scalar(
            select(ExecutionCheckpoint).where(ExecutionCheckpoint.run_id == run_id)
        )
        assert checkpoint.approval_status == "rejected"
