"""005-D：通用 ExecutionCheckpoint 接力测试。

节点重试耗尽且命中显式 fallback → 冻结 checkpoint → Recovery Agent 从剩余子图继续；
已完成节点不重放，已完成非幂等副作用调用次数不增加，未知结果需人工确认。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import WorkerService
from app.application.recovery import (
    RecoveryDriver,
    apply_recovery_decision,
    build_checkpoint,
    is_recoverable,
    verify_checkpoint,
)
from app.config import get_settings
from app.domain.workflow import WorkflowDefinition
from app.infrastructure.database import (
    ExecutionCheckpoint,
    NodeRun,
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
    # input → sensitive(side effect, 幂等) → risky(可重试失败, fallback→recovery) → output
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
    # 声明外错误码不恢复。
    assert is_recoverable("TIMEOUT", ["DEPENDENCY_UNAVAILABLE"]) is False
    # 安全/契约/未知类默认不自动恢复。
    assert is_recoverable("PERMISSION_DENIED", ["PERMISSION_DENIED"]) is False
    assert is_recoverable("UNKNOWN_OUTCOME", ["UNKNOWN_OUTCOME"]) is False


async def _seed_failed_run() -> tuple[str, str]:
    """构造一个 sensitive 成功、risky 失败（可重试耗尽）的 Run，返回 (run_id, checkpoint_id)。"""

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
            input_json={"seed": "s1"},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        # sensitive 成功一次。
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
        # risky 失败（重试耗尽）。
        risky = NodeRun(
            id=str(uuid4()),
            run_id=run.id,
            node_id="risky",
            node_type="transform",
            attempt=1,
            status="failed",
            input_json={"value": "s1"},
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
        # 已完成 sensitive 输出被冻结；已提交非幂等副作用被记录。
        assert snapshot["completedOutputs"]["sensitive"] == {"value": "s1"}
        assert snapshot["committedSideEffects"] == [{"nodeId": "sensitive", "idempotent": False}]
        # remaining 子图含失败节点，不含已完成节点。
        assert "risky" in snapshot["remainingSubgraph"]
        assert "sensitive" not in snapshot["remainingSubgraph"]
        # digest 固定内容可核验。
        verify_checkpoint(checkpoint)


async def test_recovery_resumes_remaining_without_replaying_side_effects(traex_stub: None) -> None:
    run_id, _ = await _seed_failed_run()
    driver = RecoveryDriver()
    assert await driver.poll_once() is True
    # 驱动工作流从剩余子图继续。
    worker = WorkerService("recovery-worker")
    for _ in range(4):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded", run.error_json
        # 输出来自恢复 Agent 提供的 failedOutput。
        assert run.output_json["value"] is not None
        # sensitive（非幂等副作用）仍只有一次成功 attempt，未被重放。
        from sqlalchemy import func, select

        sensitive_count = await session.scalar(
            select(func.count())
            .select_from(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == "sensitive")
        )
        assert sensitive_count == 1


async def test_recovery_request_user_transitions_to_waiting_approval() -> None:
    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(session, checkpoint, {"action": "request_user"})
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "waiting_approval"


async def test_cancel_recovering_run_marks_checkpoint_cancelled() -> None:
    """取消处于 recovering 状态的 Run：checkpoint 置 cancelled，恢复驱动不再复活它。"""

    run_id, checkpoint_id = await _seed_failed_run()
    from app.application.orchestrator import Orchestrator

    async with SessionLocal() as session, session.begin():
        await Orchestrator().request_cancellation(session, run_id)
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        # recovering 无运行中节点：取消后直接进入 cancelled 终态。
        assert run.status == "cancelled"
        # pending checkpoint 一并被取消，避免驱动 TOCTOU 复活。
        assert checkpoint.status == "cancelled"

    # 恢复驱动此时不应再领取该 checkpoint（已取消），也不会改变 Run 终态。
    driver = RecoveryDriver()
    assert await driver.poll_once() is False
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "cancelled"


async def test_build_checkpoint_is_idempotent_per_failed_attempt() -> None:
    """同一失败 attempt 只产生一个 checkpoint：致命处理重入不会重复恢复。"""

    from sqlalchemy import func, select

    from app.application.orchestrator import Orchestrator

    run_id, _ = await _seed_failed_run()
    # 再次对同一失败节点触发致命处理：不应新建第二个 checkpoint。
    async with SessionLocal() as session, session.begin():
        run = await session.get(Run, run_id)
        # 复位为 running 以便 _handle_fatal 重新评估（模拟重入）。
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
        # 幂等：仍只有最初的一个 checkpoint。
        assert count == 1
        run = await session.get(Run, run_id)
        # 已存在 checkpoint → 不再重复进入恢复，按原语义 fail run。
        assert run.status == "failed"


async def test_reapplying_decision_after_terminal_fails_closed() -> None:
    """恢复决定已应用后再次应用：digest 仍一致但状态已迁移，二次应用不复活已终态 Run。"""

    run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        await apply_recovery_decision(session, checkpoint, {"action": "abort"})
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        assert checkpoint.status == "aborted"
    # 驱动再次轮询：run 已非 recovering，不得复活。
    driver = RecoveryDriver()
    assert await driver.poll_once() is False
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"


async def test_tampered_checkpoint_fails_closed() -> None:
    _run_id, checkpoint_id = await _seed_failed_run()
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        # 篡改快照内容而不更新 digest。
        checkpoint.snapshot_json = {**checkpoint.snapshot_json, "tampered": True}
    from app.application.recovery import RecoveryError

    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
        with pytest.raises(RecoveryError):
            await apply_recovery_decision(session, checkpoint, {"action": "resume_remaining"})
