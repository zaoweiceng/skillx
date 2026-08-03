from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from app.application.orchestrator import Orchestrator, WorkerService, reconcile_run_artifacts
from app.config import get_settings
from app.domain.workflow import NodeResult
from app.infrastructure.database import (
    Artifact,
    NodeRun,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
)
from app.runtime.executors import write_json_artifact


def artifact_workflow() -> dict[str, Any]:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "artifact-staging"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
            "outputSchema": {"type": "object"},
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "agent",
                    "type": "agent",
                    "displayName": "Agent",
                    "input": {"value": "${input.value}"},
                    "config": {"maxSteps": 1},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"value": "${agent.output.value}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "agent"},
                {"source": "agent", "target": "output"},
            ],
        },
    }


async def _make_run() -> tuple[str, str]:
    definition = artifact_workflow()
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="artifact-staging",
            draft_revision=1,
            draft_json=definition,
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest="sha256:test",
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"value": "ok"})
        return run.id, version.id


def _run_root(run_id: str) -> Path:
    return (get_settings().artifact_root / run_id).resolve()


async def test_staged_artifact_promoted_only_after_cas_success() -> None:
    run_id, _ = await _make_run()
    worker = WorkerService("artifact-worker")
    # 领取 input 并推进，使 agent 就绪并被领取。
    async with SessionLocal() as session, session.begin():
        claimed_input = await worker.claim(session)
    assert claimed_input is not None
    await worker._commit_result(claimed_input, NodeResult(status="succeeded", output={}))
    async with SessionLocal() as session, session.begin():
        await Orchestrator().advance_run(session, run_id)

    async with SessionLocal() as session, session.begin():
        claimed = await worker.claim(session)
    assert claimed is not None and claimed.node_id == "agent"

    artifact = write_json_artifact(
        run_id,
        claimed.node_run_id,
        "agent",
        "trajectory.json",
        "agent-trajectory",
        {"events": [], "output": {"value": "ok"}},
    )
    # 落位前文件只在暂存目录，run 根下没有最终文件。
    assert not (_run_root(run_id) / artifact["storageKey"]).exists()

    await worker._commit_result(
        claimed,
        NodeResult(status="succeeded", output={"value": "ok"}, artifacts=[artifact]),
    )

    async with SessionLocal() as session:
        rows = (await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
    assert len(rows) == 1
    # 落位后最终文件存在，暂存目录已清空。
    assert (_run_root(run_id) / artifact["storageKey"]).is_file()
    assert not (_run_root(run_id) / ".staging" / claimed.node_run_id).exists()


async def test_late_result_after_takeover_leaves_no_orphan_file() -> None:
    run_id, _ = await _make_run()
    worker = WorkerService("artifact-worker-old", lease_seconds=1)
    async with SessionLocal() as session, session.begin():
        claimed_input = await worker.claim(session)
    assert claimed_input is not None
    await worker._commit_result(claimed_input, NodeResult(status="succeeded", output={}))
    async with SessionLocal() as session, session.begin():
        await Orchestrator().advance_run(session, run_id)

    async with SessionLocal() as session, session.begin():
        claimed = await worker.claim(session)
    assert claimed is not None and claimed.node_id == "agent"

    # 模拟租约过期后被新 Worker 接管：原 attempt 置为 lease_expired。
    async with SessionLocal() as session, session.begin():
        await session.execute(
            update(NodeRun)
            .where(NodeRun.id == claimed.node_run_id)
            .values(status="lease_expired", lease_expires_at=utcnow() - timedelta(seconds=1))
        )

    artifact = write_json_artifact(
        run_id,
        claimed.node_run_id,
        "agent",
        "trajectory.json",
        "agent-trajectory",
        {"events": [], "output": {"value": "stale"}},
    )
    # 迟到结果提交：CAS 应失败，既不登记记录也不在 run 根留下文件。
    await worker._commit_result(
        claimed,
        NodeResult(status="succeeded", output={"value": "stale"}, artifacts=[artifact]),
    )

    async with SessionLocal() as session:
        rows = (await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
    assert rows == []
    assert not (_run_root(run_id) / artifact["storageKey"]).exists()
    assert not (_run_root(run_id) / ".staging" / claimed.node_run_id).exists()


async def test_reconcile_removes_orphan_file_without_db_record() -> None:
    """DB 回滚后最终目录残留的孤儿文件应被 GC 回收。"""

    run_id, _ = await _make_run()
    run_root = _run_root(run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    orphan = run_root / "orphan-artifact"
    orphan.write_bytes(b"stale bytes")
    assert orphan.exists()
    async with SessionLocal() as session, session.begin():
        report = await reconcile_run_artifacts(session, run_id)
    assert "orphan-artifact" in report["orphanFilesRemoved"]
    assert not orphan.exists()


async def test_reconcile_recovers_missing_file_from_staging() -> None:
    """提交后落位未完成（DB 有记录、文件缺失）时，GC 从暂存补落位。"""

    run_id, _ = await _make_run()
    node_run_id = str(uuid4())
    # 写入暂存文件但不落位，模拟“提交后进程崩溃、落位未执行”。
    artifact = write_json_artifact(
        run_id, node_run_id, "agent", "trajectory.json", "agent-trajectory", {"v": 1}
    )
    async with SessionLocal() as session, session.begin():
        session.add(
            Artifact(
                id=artifact["id"],
                run_id=run_id,
                kind=artifact["kind"],
                name=artifact["name"],
                uri=artifact["uri"],
                sha256=artifact["sha256"],
                size=artifact["size"],
                media_type=artifact["mediaType"],
                metadata_json={
                    "storageKey": artifact["storageKey"],
                    "stagingKey": artifact["stagingKey"],
                },
            )
        )
    # 落位前最终文件缺失。
    assert not (_run_root(run_id) / artifact["storageKey"]).exists()
    async with SessionLocal() as session, session.begin():
        report = await reconcile_run_artifacts(session, run_id)
    assert artifact["id"] in report["recovered"]
    assert (_run_root(run_id) / artifact["storageKey"]).is_file()


async def test_reconcile_reports_unrecoverable_missing_file() -> None:
    """暂存与最终都缺失时报告为需人工处理的缺失，不误删记录。"""

    run_id, _ = await _make_run()
    async with SessionLocal() as session, session.begin():
        session.add(
            Artifact(
                id=str(uuid4()),
                run_id=run_id,
                kind="report",
                name="gone.json",
                uri="artifact://gone",
                sha256="0" * 64,
                size=0,
                media_type="application/json",
                metadata_json={"storageKey": "gone"},
            )
        )
    async with SessionLocal() as session, session.begin():
        report = await reconcile_run_artifacts(session, run_id)
    assert len(report["missing"]) == 1
