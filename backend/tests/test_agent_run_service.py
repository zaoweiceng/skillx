import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.routes import (
    AgentRunCreate,
    TaskCheckpointAnswer,
    answer_task_checkpoint,
    cancel_conversation_run,
    distill_run,
    download_artifact,
    get_conversation_run,
    list_conversation_run_events,
    start_agent_run,
)
from app.application.agent_host import AgentHostError
from app.application.agent_run import (
    AgentRunConfig,
    AgentRunWorker,
    enqueue_agent_run,
    resolve_effective_capabilities,
)
from app.application.orchestrator import reconcile_conversation_artifacts
from app.application.task_recovery import TaskCheckpointDriver
from app.config import get_settings
from app.infrastructure.database import (
    Artifact,
    Conversation,
    ConversationRun,
    ConversationRunEvent,
    SessionLocal,
    TaskCheckpoint,
    utcnow,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


@pytest.fixture
def traex_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(settings, "traex_allowed_executables", [Path(sys.executable).name])
    monkeypatch.setattr(settings, "agent_host_capabilities", ["search.web", "lookup.table"])


async def _conversation() -> str:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="通用 Agent")
        session.add(conversation)
        await session.flush()
        return conversation.id


async def _enqueue(conversation_id: str, content: str, **kwargs) -> str:
    async with SessionLocal() as session, session.begin():
        conversation = await session.get(Conversation, conversation_id)
        run = await enqueue_agent_run(session, conversation, content, **kwargs)
        return run.id


async def _drive(run_id: str) -> ConversationRun:
    worker = AgentRunWorker("test-agent-worker")
    for _ in range(3):
        if not await worker.poll_once():
            break
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        assert run is not None
        return run


def test_effective_capabilities_is_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_host_capabilities", ["search.web"])
    effective, denied = resolve_effective_capabilities(("search.web", "banned.tool"))
    assert effective == ["search.web"]
    assert denied == ["banned.tool"]


async def test_enqueue_creates_queued_run_without_executing(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id, "完成任务 [[MODE:ok]][[REPLY:分类完成]]"
    )
    # 入队即持久化，尚未执行：状态 queued，仅有 created 事件。
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        assert run.status == "queued"
        events = list(
            await session.scalars(
                select(ConversationRunEvent.type).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
        assert events == ["agent.run.created"]


async def test_worker_executes_queued_run_to_success(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:ok]][[REPLY:分类完成]]")
    run = await _drive(run_id)
    assert run.status == "succeeded"
    assert run.run_type == "agent_run"
    assert run.output_json == {"reply": "分类完成"}
    async with SessionLocal() as session:
        event_types = set(
            await session.scalars(
                select(ConversationRunEvent.type).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
        assert {"agent.run.created", "agent.run.started", "agent.plan", "agent.run.succeeded"} <= (
            event_types
        )


async def test_worker_rejects_unauthorized_tool(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:unauthorized_tool]]")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "PERMISSION_DENIED"


async def test_worker_rejects_missing_output(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "完成任务 [[MODE:no_output]]")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "EXECUTION_FAILED"


async def test_worker_enforces_step_budget(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "完成任务 [[MODE:many_steps]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            max_steps=3,
            allowed_capabilities=("search.web",),
        ),
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "STEP_BUDGET_EXCEEDED"


async def test_worker_enforces_token_budget(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "完成任务 [[MODE:big_tokens]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            max_total_tokens=1000,
            allowed_capabilities=("search.web",),
        ),
    )
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "TOKEN_BUDGET_EXCEEDED"


async def test_running_events_visible_before_terminal(traex_stub: None) -> None:
    """另一个连接在运行完成前能看到 committed 的运行中事件。"""

    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "长任务 [[MODE:hang]]")
    worker = AgentRunWorker("running-visibility-worker")
    task = asyncio.create_task(worker.poll_once())
    try:
        # 轮询独立连接，直到出现 running 状态与运行中事件。
        for _ in range(50):
            await asyncio.sleep(0.1)
            async with SessionLocal() as session:
                run = await session.get(ConversationRun, run_id)
                events = list(
                    await session.scalars(
                        select(ConversationRunEvent.type).where(
                            ConversationRunEvent.conversation_run_id == run_id
                        )
                    )
                )
            if run.status == "running" and "agent.run.started" in events:
                break
        assert run.status == "running"
        assert "agent.run.started" in events
        # 请求取消，Worker 应把 hang 子进程终止并写入 cancelled 终态。
        async with SessionLocal() as session, session.begin():
            from app.application.agent_run import request_cancellation

            await request_cancellation(session, run_id)
    finally:
        await asyncio.wait_for(task, timeout=40)
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
    assert run.status == "cancelled"


async def test_cancel_queued_run_never_executes(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:x]]")
    async with SessionLocal() as session:
        run = await cancel_conversation_run(run_id, session)
    assert run["status"] == "cancelled"
    # Worker 领取时不再执行已取消的 run。
    worker = AgentRunWorker("cancel-worker")
    processed = await worker.poll_once()
    assert processed is False
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
    assert run.status == "cancelled"


async def test_recover_expired_lease_marks_unknown_outcome(traex_stub: None) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:x]]")
    # 模拟一个被领取但租约过期、结果未知的 running 运行。
    async with SessionLocal() as session, session.begin():
        run = await session.get(ConversationRun, run_id)
        run.status = "running"
        run.lease_owner = "dead-worker"
        run.lease_expires_at = utcnow() - timedelta(hours=1)
    worker = AgentRunWorker("recover-worker")
    async with SessionLocal() as session, session.begin():
        recovered = await worker.recover_expired(session)
    assert recovered == 1
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        checkpoint = await session.scalar(
            select(TaskCheckpoint).where(TaskCheckpoint.source_id == run_id)
        )
    assert run.status == "failed"
    assert run.error_json["code"] == "UNKNOWN_OUTCOME"
    assert checkpoint is not None
    assert checkpoint.status == "awaiting_user"
    assert checkpoint.snapshot_json["outcomeCertainty"] == "unknown"

    async with SessionLocal() as session:
        answered = await answer_task_checkpoint(
            checkpoint.id,
            TaskCheckpointAnswer(
                snapshotDigest=checkpoint.digest,
                answer={"continue": True, "externalStateChecked": True},
            ),
            session,
        )
    assert answered["status"] == "pending"
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await answer_task_checkpoint(
                checkpoint.id,
                TaskCheckpointAnswer(
                    snapshotDigest=checkpoint.digest,
                    answer={"continue": True},
                ),
                session,
            )
    assert exc.value.status_code == 409


async def test_task_checkpoint_claims_and_continues_from_tool_artifact(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "q1 [[MODE:deterministic_fail_after_tool]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            allowed_capabilities=("lookup.table",),
        ),
    )
    source = await _drive(run_id)
    assert source.status == "failed"
    async with SessionLocal() as session:
        checkpoint = await session.scalar(
            select(TaskCheckpoint).where(TaskCheckpoint.source_id == run_id)
        )
        artifacts = list(
            await session.scalars(
                select(Artifact).where(
                    Artifact.conversation_run_id == run_id,
                    Artifact.kind == "tool_result",
                )
            )
        )
    assert checkpoint is not None and checkpoint.status == "pending"
    assert len(artifacts) == 1
    assert checkpoint.snapshot_json["completedSteps"][-1]["artifact"]["id"] == (
        artifacts[0].id
    )

    driver = TaskCheckpointDriver("test-task-recovery")
    assert await driver.poll_once()
    async with SessionLocal() as session:
        checkpoint = await session.get(TaskCheckpoint, checkpoint.id)
        continuation_id = checkpoint.snapshot_json["continuationRunId"]
    assert await AgentRunWorker("test-continuation-agent").poll_once()
    assert await driver.poll_once()

    async with SessionLocal() as session:
        source = await session.get(ConversationRun, run_id)
        continuation = await session.get(ConversationRun, continuation_id)
        checkpoint = await session.get(TaskCheckpoint, checkpoint.id)
    assert source.status == "failed"
    assert continuation.status == "succeeded"
    assert continuation.output_json == {"reply": "第一季度：营收增长 12%"}
    assert continuation.input_json["continuationOfCheckpointId"] == checkpoint.id
    assert checkpoint.status == "succeeded"


async def test_task_checkpoint_late_same_owner_cannot_start_second_continuation(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "q1 [[MODE:deterministic_fail_after_tool]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            allowed_capabilities=("lookup.table",),
        ),
    )
    await _drive(run_id)
    driver_a = TaskCheckpointDriver("same-owner", lease_seconds=1)
    driver_b = TaskCheckpointDriver("same-owner", lease_seconds=1)
    async with SessionLocal() as session, session.begin():
        handle_a = await driver_a.claim(session)
    assert handle_a is not None
    async with SessionLocal() as session, session.begin():
        checkpoint = await session.get(TaskCheckpoint, handle_a.id)
        checkpoint.lease_expires_at = utcnow() - timedelta(seconds=1)
    async with SessionLocal() as session, session.begin():
        handle_b = await driver_b.claim(session)
    assert handle_b is not None
    assert handle_b.generation == handle_a.generation + 1
    assert await driver_a.start_continuation(handle_a) is False
    assert await driver_b.start_continuation(handle_b) is True
    assert await driver_a.start_continuation(handle_a) is False

    async with SessionLocal() as session:
        continuations = list(
            await session.scalars(
                select(ConversationRun).where(
                    ConversationRun.input_json["continuationOfCheckpointId"]
                    .as_string()
                    == handle_a.id
                )
            )
        )
        started = list(
            await session.scalars(
                select(ConversationRunEvent).where(
                    ConversationRunEvent.conversation_run_id == run_id,
                    ConversationRunEvent.type == "agent.handoff.started",
                )
            )
        )
    assert len(continuations) == 1
    assert len(started) == 1


async def test_tool_result_duplicate_call_id_requires_identical_hash(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "q1")
    worker = AgentRunWorker("artifact-owner")
    async with SessionLocal() as session, session.begin():
        assert await worker.claim(session) == run_id
    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        generation = run.claim_generation
        token = run.claim_token
    payload = {
        "callId": "duplicate",
        "capabilityRef": "lookup.table",
        "status": "succeeded",
        "output": {"value": "first"},
        "evidenceDigest": "sha256:" + "1" * 64,
        "effect": "none",
        "idempotent": True,
        "actionSignature": "sha256:" + "2" * 64,
    }
    await worker._persist_tool_result_artifact(
        run_id, payload, generation, token
    )
    await worker._persist_tool_result_artifact(
        run_id, payload, generation, token
    )
    with pytest.raises(AgentHostError, match="hash 不一致"):
        await worker._persist_tool_result_artifact(
            run_id,
            {**payload, "output": {"value": "conflict"}},
            generation,
            token,
        )
    async with SessionLocal() as session:
        artifacts = list(
            await session.scalars(
                select(Artifact).where(
                    Artifact.conversation_run_id == run_id,
                    Artifact.kind == "tool_result",
                )
            )
        )
    assert len(artifacts) == 1


async def test_reconcile_removes_unregistered_tool_result_staging() -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "staging crash")
    async with SessionLocal() as session, session.begin():
        run = await session.get(ConversationRun, run_id)
        run.status = "failed"
    root = (
        get_settings().artifact_root
        / "conversation-agents"
        / run_id
    )
    orphan = root / ".staging/crashed/tool-results/orphan.json"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text('{"orphan":true}', encoding="utf-8")

    async with SessionLocal() as session, session.begin():
        report = await reconcile_conversation_artifacts(session, run_id)

    assert ".staging/crashed/tool-results/orphan.json" in report[
        "orphanStagingRemoved"
    ]
    assert not orphan.exists()
    assert not (root / "tool-results/orphan.json").exists()


async def test_late_owner_cannot_commit_output_or_artifact(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务")
    old_worker = AgentRunWorker("old-owner")
    async with SessionLocal() as session, session.begin():
        assert await old_worker.claim(session) == run_id
    async with SessionLocal() as session, session.begin():
        run = await session.get(ConversationRun, run_id)
        old_generation = run.claim_generation
        old_token = run.claim_token
        run.lease_owner = "new-owner"
        run.claim_generation += 1
        run.claim_token = str(uuid4())

    await old_worker._finalize_success(
        run_id,
        AgentRunConfig.default(),
        "任务",
        {"reply": "迟到结果"},
        [],
        [],
        1,
        1,
        old_generation,
        old_token,
    )

    async with SessionLocal() as session:
        run = await session.get(ConversationRun, run_id)
        artifacts = list(
            await session.scalars(
                select(Artifact).where(Artifact.conversation_run_id == run_id)
            )
        )
    assert run.status == "running"
    assert run.output_json == {}
    assert artifacts == []


async def test_agent_endpoint_returns_202_and_is_distillable(traex_stub: None) -> None:
    conversation_id = await _conversation()
    async with SessionLocal() as session:
        response = await start_agent_run(
            conversation_id,
            AgentRunCreate(content="完成任务 [[MODE:ok]][[REPLY:探索结果]]"),
            session,
        )
    assert response["status"] == "queued"
    conversation_run_id = response["conversationRunId"]

    run = await _drive(conversation_run_id)
    assert run.status == "succeeded"

    async with SessionLocal() as session:
        events = await list_conversation_run_events(conversation_run_id, 0, session)
    assert any(item["type"] == "agent.run.succeeded" for item in events)

    async with SessionLocal() as session:
        summary = await get_conversation_run(conversation_run_id, session)
    assert summary["runType"] == "agent_run"
    assert summary["output"] == {"reply": "探索结果"}

    async with SessionLocal() as session:
        distilled = await distill_run(conversation_run_id, session)
    assert distilled["status"] == "draft_created"
    assert distilled["analysis"]["sourceRunType"] == "agent_run"


async def test_run_not_configured_records_failure() -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任意任务")
    run = await _drive(run_id)
    assert run.status == "failed"
    assert run.error_json["code"] == "MODEL_NOT_CONFIGURED"


async def test_secret_never_appears_in_trajectory_or_disk(traex_stub: None) -> None:
    """E-02：Secret 原值不得出现在 trajectory_json、事件 payload 或落盘轨迹字节中。"""

    conversation_id = await _conversation()
    run_id = await _enqueue(
        conversation_id,
        "查询 [[MODE:secret_result]][[REPLY:ok]]",
        config=AgentRunConfig(
            output_schema=AgentRunConfig.default().output_schema,
            allowed_capabilities=("lookup.table",),
        ),
    )
    run = await _drive(run_id)
    assert run.status == "succeeded"
    leaked = ("sk-should-not-leak", "p@ss-should-not-leak")
    async with SessionLocal() as session:
        row = await session.get(ConversationRun, run_id)
        trajectory_text = json.dumps(row.trajectory_json, ensure_ascii=False)
        events = list(
            await session.scalars(
                select(ConversationRunEvent.payload_json).where(
                    ConversationRunEvent.conversation_run_id == run_id
                )
            )
        )
    events_text = json.dumps(events, ensure_ascii=False)
    for secret in leaked:
        assert secret not in trajectory_text
        assert secret not in events_text
    # 扫描落盘轨迹字节。
    directory = get_settings().artifact_root / "conversation-agents" / run_id
    for path in directory.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for secret in leaked:
                assert secret.encode() not in content


async def test_conversation_trajectory_download_reconcile_and_integrity(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:证据]]")
    run = await _drive(run_id)
    assert run.status == "succeeded"
    async with SessionLocal() as session:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.conversation_run_id == run_id)
        )
        response = await download_artifact(artifact.id, session)
    target = Path(response.path)
    assert target.is_file()

    staging = target.parent / artifact.metadata_json["stagingKey"]
    staging.parent.mkdir(parents=True, exist_ok=True)
    target.replace(staging)
    async with SessionLocal() as session, session.begin():
        report = await reconcile_conversation_artifacts(session, run_id)
    assert artifact.id in report["recovered"]
    assert target.is_file()

    target.write_bytes(b"tampered")
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as exc:
            await download_artifact(artifact.id, session)
    assert exc.value.status_code == 409
    async with SessionLocal() as session, session.begin():
        report = await reconcile_conversation_artifacts(session, run_id)
    assert artifact.id in report["corrupt"]
    assert not target.exists()


async def test_reconcile_rejects_escaped_storage_key(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:证据]]")
    run = await _drive(run_id)
    assert run.status == "succeeded"
    async with SessionLocal() as session, session.begin():
        artifact = await session.scalar(
            select(Artifact).where(Artifact.conversation_run_id == run_id)
        )
        artifact.metadata_json = {
            **artifact.metadata_json,
            "storageKey": "../../escaped.json",
        }
        artifact_id = artifact.id
    async with SessionLocal() as session, session.begin():
        report = await reconcile_conversation_artifacts(session, run_id)
    assert artifact_id in report["pathRejected"]


async def test_reconcile_rejects_corrupt_staging(
    traex_stub: None,
) -> None:
    conversation_id = await _conversation()
    run_id = await _enqueue(conversation_id, "任务 [[MODE:ok]][[REPLY:证据]]")
    run = await _drive(run_id)
    assert run.status == "succeeded"
    async with SessionLocal() as session:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.conversation_run_id == run_id)
        )
    root = get_settings().artifact_root / "conversation-agents" / run_id
    target = root / artifact.metadata_json["storageKey"]
    staging = root / artifact.metadata_json["stagingKey"]
    staging.parent.mkdir(parents=True, exist_ok=True)
    target.replace(staging)
    staging.write_bytes(b"corrupt")

    async with SessionLocal() as session, session.begin():
        report = await reconcile_conversation_artifacts(session, run_id)

    assert artifact.id in report["corrupt"]
    assert artifact.id not in report["recovered"]
    assert not staging.exists()
    assert not target.exists()
