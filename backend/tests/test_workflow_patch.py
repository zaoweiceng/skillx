"""005-E/006-D：失败可复现性、受控 WorkflowPatch DSL、后端 eligibility 与真实 replay。"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import routes
from app.api.routes import (
    DraftUpdate,
    PublishRequest,
    apply_version_workflow_patch,
    propose_failure_revision,
    publish_workflow,
    replay_dirty_revision_draft,
    update_draft,
)
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.application.workflow_catalog import (
    resolve_workflow_manifest,
    search_manifests,
)
from app.application.workflow_patch import (
    REPRODUCIBLE_THRESHOLD,
    WorkflowPatch,
    WorkflowPatchViolation,
    apply_workflow_patch,
    classify_failure,
    failure_fingerprint,
    observation_eligibility,
    record_failure_observation,
    should_propose_patch,
)
from app.config import get_settings
from app.infrastructure.database import (
    FailureObservation,
    HumanPatch,
    ReplayEvidence,
    ReplayFixture,
    RevisionProposal,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
)

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


@pytest.fixture
def revision_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "traex_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(
        settings, "traex_allowed_executables", [Path(sys.executable).name]
    )

def test_fingerprint_stable_and_excludes_noise() -> None:
    a = failure_fingerprint(
        workflow_version_id="v1",
        node_id="risky",
        node_type="mcp",
        error_code="MCP_CONTRACT_DRIFT",
        capability_lock="sha256:abc",
    )
    b = failure_fingerprint(
        workflow_version_id="v1",
        node_id="risky",
        node_type="mcp",
        error_code="MCP_CONTRACT_DRIFT",
        capability_lock="sha256:abc",
    )
    # 相同稳定输入 → 相同指纹（不受时间戳/随机 id 影响）。
    assert a == b
    # 错误码变化 → 指纹变化。
    c = failure_fingerprint(
        workflow_version_id="v1",
        node_id="risky",
        node_type="mcp",
        error_code="TIMEOUT",
        capability_lock="sha256:abc",
    )
    assert a != c


def test_transient_failure_produces_no_patch() -> None:
    cls = classify_failure("DEPENDENCY_UNAVAILABLE", recovered=True, reproducible=False)
    assert cls == "transient"
    assert should_propose_patch(cls, reproducible=False) is False


def test_reproducible_contract_drift_proposes_patch() -> None:
    cls = classify_failure("MCP_CONTRACT_DRIFT", recovered=False, reproducible=True)
    assert cls == "contract_drift"
    assert should_propose_patch(cls, reproducible=True) is True


def _definition() -> dict:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "patchable"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "outputSchema": {"type": "object", "properties": {"y": {"type": "string"}}},
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "work",
                    "type": "transform",
                    "displayName": "处理",
                    "input": {"value": "${input.x}"},
                    "config": {"operation": "passthrough"},
                    "timeoutSeconds": 30,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"y": "${work.output.value}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "work"},
                {"source": "work", "target": "output"},
            ],
        },
    }


def test_patch_adjusts_allowed_field() -> None:
    patch = WorkflowPatch.model_validate(
        {
            "baseVersionId": "v1",
            "nodePatches": [{"nodeId": "work", "changes": {"timeoutSeconds": 60}}],
        }
    )
    patched = apply_workflow_patch(_definition(), patch)
    work = next(n for n in patched["spec"]["nodes"] if n["id"] == "work")
    assert work["timeoutSeconds"] == 60


def test_patch_rejects_forbidden_field() -> None:
    # 不得修改 type（越出受限字段集合）。
    patch = WorkflowPatch.model_validate(
        {"baseVersionId": "v1", "nodePatches": [{"nodeId": "work", "changes": {"type": "agent"}}]}
    )
    with pytest.raises(WorkflowPatchViolation):
        apply_workflow_patch(_definition(), patch)


def test_patch_rejects_secret_and_host_path() -> None:
    patch = WorkflowPatch.model_validate(
        {
            "baseVersionId": "v1",
            "nodePatches": [
                {"nodeId": "work", "changes": {"config": {"command": ["/bin/sh"]}}}
            ],
        }
    )
    with pytest.raises(WorkflowPatchViolation):
        apply_workflow_patch(_definition(), patch)


def test_patch_rejects_capability_lock_expansion() -> None:
    # 补丁不得扩权：allowedCapabilities / mcpToolSnapshotId / model 等能力锁字段被封死。
    for forbidden in (
        {"allowedCapabilities": ["net.write"]},
        {"mcpToolSnapshotId": "snap-evil"},
        {"model": "gpt-evil"},
    ):
        patch = WorkflowPatch.model_validate(
            {
                "baseVersionId": "v1",
                "nodePatches": [{"nodeId": "work", "changes": {"config": forbidden}}],
            }
        )
        with pytest.raises(WorkflowPatchViolation):
            apply_workflow_patch(_definition(), patch)


def test_patch_rejects_fallback_field() -> None:
    # fallback 不在受限字段集合内：不得通过普通 Patch 修改恢复策略。
    patch = WorkflowPatch.model_validate(
        {
            "baseVersionId": "v1",
            "nodePatches": [
                {"nodeId": "work", "changes": {"fallback": {"on": ["X"], "target": "y"}}}
            ],
        }
    )
    with pytest.raises(WorkflowPatchViolation):
        apply_workflow_patch(_definition(), patch)


def test_observation_eligibility_requires_reproducibility() -> None:
    obs = FailureObservation(
        id="o1",
        fingerprint="fp",
        workflow_version_id="v1",
        node_id="work",
        node_type="transform",
        error_code="INVALID_INPUT",
        classification="deterministic_input",
        occurrence=1,
        recovered=False,
        contract_summary_json={},
    )
    # occurrence 只表示热度，不能替代 baseline reproduction。
    assert observation_eligibility(obs)["eligible"] is False
    obs.occurrence = REPRODUCIBLE_THRESHOLD
    assert observation_eligibility(obs)["eligible"] is False
    obs.baseline_reproduced = True
    result = observation_eligibility(obs)
    assert result["eligible"] is True
    assert result["reproducible"] is True


async def _seed_version_with_failure(
    *,
    error_code: str,
    occurrence: int,
    node_id: str = "work",
    real_failure: bool = False,
) -> tuple[str, str]:
    """建版本 + source Run + 指定 occurrence 的 FailureObservation。返回 (version_id, run_id)。"""

    async with SessionLocal() as session, session.begin():
        definition = _definition()
        if real_failure:
            definition["spec"]["nodes"][1]["config"]["operation"] = "fail_dependency"
        workflow = Workflow(
            id=str(uuid4()), name="wf", draft_revision=1, draft_json=definition
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest="sha256:x",
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = Run(
            id=str(uuid4()),
            workflow_version_id=version.id,
            status="failed",
            input_json={"x": "safe"},
            started_at=utcnow(),
        )
        session.add(run)
        await session.flush()
        for _ in range(occurrence):
            await record_failure_observation(
                session,
                workflow_version_id=version.id,
                node_id=node_id,
                node_type="transform",
                error_code=error_code,
                run_id=run.id,
            )
        return version.id, run.id


async def test_patch_endpoint_rejects_when_no_observation() -> None:
    version_id, _run_id = await _seed_version_with_failure(
        error_code="INVALID_INPUT", occurrence=0
    )
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as excinfo:
            await apply_version_workflow_patch(
                version_id,
                {
                    "sourceRunId": "nonexistent",
                    "patch": {
                        "nodePatches": [{"nodeId": "work", "changes": {"timeoutSeconds": 90}}]
                    },
                },
                session,
            )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "REVISION_PROPOSAL_MISMATCH"


async def test_revision_agent_rejects_historical_transport_fixture(
    revision_agent: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def unexpected_agent_call(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("transport fixture 不得调用 Revision Agent")

    monkeypatch.setattr(
        routes.AgentHost,
        "execute",
        unexpected_agent_call,
    )
    _version_id, run_id = await _seed_version_with_failure(
        error_code="DEPENDENCY_UNAVAILABLE",
        occurrence=1,
        real_failure=True,
    )
    async with SessionLocal() as session:
        observation = await session.scalar(
            select(FailureObservation).where(
                FailureObservation.last_run_id == run_id
            )
        )
        with pytest.raises(HTTPException) as excinfo:
            await propose_failure_revision(observation.id, session)
    assert excinfo.value.detail["code"] == "REVISION_NOT_ELIGIBLE"
    assert "transport/timeout" in excinfo.value.detail["message"]
    assert calls == 0


async def test_patch_endpoint_rejects_transient_single_occurrence() -> None:
    # 一次 TIMEOUT（瞬态、未达阈值）→ 不 eligible。
    version_id, run_id = await _seed_version_with_failure(
        error_code="TIMEOUT", occurrence=1
    )
    async with SessionLocal() as session:
        observation = await session.scalar(
            select(FailureObservation).where(
                FailureObservation.last_run_id == run_id
            )
        )
        with pytest.raises(HTTPException) as excinfo:
            await propose_failure_revision(observation.id, session)
        proposals = list(
            await session.scalars(
                select(RevisionProposal).where(
                    RevisionProposal.base_version_id == version_id
                )
            )
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "REVISION_NOT_ELIGIBLE"
    assert proposals == []


async def test_patch_endpoint_rejects_replayed_transport_error() -> None:
    # 即使同一历史 Run 被重复登记，transport fixture 也只能用于诊断。
    version_id, run_id = await _seed_version_with_failure(
        error_code="DEPENDENCY_UNAVAILABLE",
        occurrence=REPRODUCIBLE_THRESHOLD,
        real_failure=True,
    )
    async with SessionLocal() as session:
        observation = await session.scalar(
            select(FailureObservation).where(
                FailureObservation.last_run_id == run_id
            )
        )
        with pytest.raises(HTTPException) as excinfo:
            await propose_failure_revision(observation.id, session)
        proposals = list(
            await session.scalars(
                select(RevisionProposal).where(
                    RevisionProposal.base_version_id == version_id
                )
            )
        )
    assert excinfo.value.detail["code"] == "REVISION_NOT_ELIGIBLE"
    assert "transport/timeout" in excinfo.value.detail["message"]
    assert proposals == []


class _ArticleHandler(BaseHTTPRequestHandler):
    content = "external-A"

    def do_GET(self) -> None:
        if not self.path.startswith("/article"):
            self.send_error(404)
            return
        encoded = f"<main id='main'>{self.content}</main>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def article_site() -> Iterator[str]:
    _ArticleHandler.content = "external-A"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArticleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/article"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _browser_contract_failure_definition(url: str) -> dict:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "external-browser-contract"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string"},
                },
            },
            "outputSchema": {
                "type": "object",
                "required": ["content"],
                "properties": {"content": {}},
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "read",
                    "type": "browser",
                    "displayName": "外部读取",
                    "input": {"url": "${input.url}"},
                    "config": {
                        "action": "read",
                        "runtime": "playwright",
                        "pageContract": {
                            "contractVersion": "1",
                            "urlPattern": f"{url}*",
                            "bodySelector": "#main",
                            "fields": {},
                            "outputSchema": {
                                "type": "object",
                                "required": ["content"],
                                "properties": {"content": {"type": "integer"}},
                            },
                        },
                    },
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"content": "${read.output.content}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "read"},
                {"source": "read", "target": "output"},
            ],
        },
    }


async def test_publish_same_reviewed_snapshot_is_concurrent_idempotent() -> None:
    definition = _definition()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="publish-cas",
            draft_revision=1,
            draft_json=definition,
            draft_digest=compiled.digest,
            draft_metadata_json={"status": "manual_draft"},
        )
        session.add(workflow)
        workflow_id = workflow.id
    request = PublishRequest(
        draftRevision=1,
        draftDigest=compiled.digest,
    )

    async def publish_once() -> dict[str, Any]:
        async with SessionLocal() as session:
            return await publish_workflow(workflow_id, request, session)

    first, second = await asyncio.gather(publish_once(), publish_once())
    assert first["id"] == second["id"]
    async with SessionLocal() as session:
        versions = list(
            await session.scalars(
                select(WorkflowVersion).where(
                    WorkflowVersion.workflow_id == workflow_id
                )
            )
        )
    assert len(versions) == 1


async def test_external_browser_failure_fixture_patch_publish_and_reuse(
    article_site: str,
    revision_agent: None,
) -> None:
    definition = _browser_contract_failure_definition(article_site)
    original_input = {
        "url": f"{article_site}?version=a",
    }
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="external-browser-contract",
            draft_revision=1,
            draft_json=definition,
        )
        session.add(workflow)
        await session.flush()
        workflow_id = workflow.id
    async with SessionLocal() as session:
        workflow = await session.get(Workflow, workflow_id)
        published_v1 = await publish_workflow(
            workflow_id,
            PublishRequest(
                draftRevision=workflow.draft_revision,
                draftDigest=WorkflowCompiler().compile(
                    workflow.draft_json
                ).digest,
            ),
            session,
        )
    version_id = published_v1["id"]
    v1_ref = published_v1["workflowRef"]
    worker = WorkerService("external-failure-worker")
    source_run_ids = []
    for _ in range(REPRODUCIBLE_THRESHOLD):
        async with SessionLocal() as session, session.begin():
            version = await session.get(WorkflowVersion, version_id)
            run = await Orchestrator().create_run(
                session, version, original_input
            )
            source_run_ids.append(run.id)
        for _ in range(5):
            await worker.run_once()
    source_run_id = source_run_ids[-1]
    async with SessionLocal() as session:
        source_run = await session.get(Run, source_run_id)
        fixture = await session.scalar(
            select(ReplayFixture).where(ReplayFixture.run_id == source_run_id)
        )
    assert source_run.status == "failed"
    assert source_run.error_json["code"] == "CONTRACT_VIOLATION"
    assert fixture is not None
    assert fixture.capture_policy.startswith("failure_")
    assert fixture.evidence_kind == "observed_contract_output"
    async with SessionLocal() as session:
        observation = await session.scalar(
            select(FailureObservation).where(
                FailureObservation.last_run_id == source_run_id
            )
        )
        proposal = await propose_failure_revision(observation.id, session)
    assert proposal["proposal"]["nodePatches"][0]["nodeId"] == "read"
    assert proposal["proposal"]["nodePatches"][0]["changes"]["config"][
        "pageContract"
    ]["outputSchema"]["properties"]["content"]["type"] == "string"
    async with SessionLocal() as session:
        reused_proposal = await propose_failure_revision(
            observation.id, session
        )
    assert reused_proposal["proposalId"] == proposal["proposalId"]
    tampered = deepcopy(proposal["proposal"])
    tampered["reason"] = "client changed this proposal"
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as tamper_error:
            await apply_version_workflow_patch(
                version_id,
                {
                    "sourceRunId": source_run_id,
                    "proposalId": proposal["proposalId"],
                    "proposalDigest": proposal["proposalDigest"],
                    "patch": tampered,
                },
                session,
            )
    assert tamper_error.value.status_code == 409
    assert tamper_error.value.detail["code"] == "REVISION_PROPOSAL_TAMPERED"
    async with SessionLocal() as session:
        proof = await apply_version_workflow_patch(
            version_id,
            {
                "sourceRunId": source_run_id,
                "proposalId": proposal["proposalId"],
                "proposalDigest": proposal["proposalDigest"],
                "patch": proposal["proposal"],
            },
            session,
        )
    assert proof["replay"]["baselineReproduced"] is True
    assert proof["replay"]["passed"] is True
    assert proof["draftWorkflowId"] == workflow_id
    assert proof["expectedNextVersion"] == 2
    assert proof["draftMetadata"]["proposalDigest"] == proposal[
        "proposalDigest"
    ]
    async with SessionLocal() as session:
        unchanged = await update_draft(
            proof["draftWorkflowId"],
            DraftUpdate(
                revision=proof["draftRevision"],
                definition=deepcopy(proof["definition"]),
            ),
            session,
        )
    assert unchanged["draftRevision"] == proof["draftRevision"]
    assert unchanged["draftMetadata"]["status"] == "pending_revision"
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as conflict:
            await apply_version_workflow_patch(
                version_id,
                {
                    "sourceRunId": source_run_id,
                    "proposalId": proposal["proposalId"],
                    "proposalDigest": proposal["proposalDigest"],
                    "patch": proposal["proposal"],
                },
                session,
            )
    assert conflict.value.status_code == 409
    edited_definition = deepcopy(proof["definition"])
    read_node = next(
        node
        for node in edited_definition["spec"]["nodes"]
        if node["id"] == "read"
    )
    read_node["timeoutSeconds"] = 45
    async with SessionLocal() as session:
        dirty = await update_draft(
            proof["draftWorkflowId"],
            DraftUpdate(
                revision=proof["draftRevision"],
                definition=edited_definition,
            ),
            session,
        )
    assert dirty["draftMetadata"]["status"] == "dirty_after_replay"
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as old_snapshot:
            await publish_workflow(
                proof["draftWorkflowId"],
                PublishRequest(
                    draftRevision=proof["draftRevision"],
                    draftDigest=proof["draftDigest"],
                    replayEvidenceId=proof["replay"]["replayEvidenceId"],
                ),
                session,
            )
    assert old_snapshot.value.detail["code"] == "DRAFT_SNAPSHOT_CONFLICT"
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as stale_proof:
            await publish_workflow(
                proof["draftWorkflowId"],
                PublishRequest(
                    draftRevision=dirty["draftRevision"],
                    draftDigest=dirty["draftDigest"],
                    replayEvidenceId=proof["replay"]["replayEvidenceId"],
                ),
                session,
            )
    assert stale_proof.value.detail["code"] == "REPLAY_PROOF_STALE"
    async with SessionLocal() as session:
        approved = await replay_dirty_revision_draft(
            proof["draftWorkflowId"], session
        )
    assert approved["replay"]["passed"] is True
    assert (
        approved["replay"]["replayEvidenceId"]
        != proof["replay"]["replayEvidenceId"]
    )
    async with SessionLocal() as session:
        published = await publish_workflow(
            proof["draftWorkflowId"],
            PublishRequest(
                draftRevision=approved["draftRevision"],
                draftDigest=approved["draftDigest"],
                replayEvidenceId=approved["replay"]["replayEvidenceId"],
            ),
            session,
        )
        repeated = await publish_workflow(
            proof["draftWorkflowId"],
            PublishRequest(
                draftRevision=approved["draftRevision"],
                draftDigest=approved["draftDigest"],
                replayEvidenceId=approved["replay"]["replayEvidenceId"],
            ),
            session,
        )
    assert published["workflowId"] == workflow_id
    assert published["version"] == 2
    assert repeated["id"] == published["id"]
    async with SessionLocal() as session:
        proposal_record = await session.get(
            RevisionProposal, proposal["proposalId"]
        )
        human_patch = await session.get(
            HumanPatch, approved["draftMetadata"]["humanPatchId"]
        )
        replay_evidence = await session.get(
            ReplayEvidence, approved["replay"]["replayEvidenceId"]
        )
        published_record = await session.get(
            WorkflowVersion, published["id"]
        )
        cards = await search_manifests(
            session, "external-browser-contract"
        )
        old_manifest = await resolve_workflow_manifest(session, v1_ref)
    assert proposal_record.proposal_digest == proposal["proposalDigest"]
    assert replay_evidence.human_patch_id == human_patch.id
    assert replay_evidence.patch_digest == human_patch.patch_digest
    assert replay_evidence.patched_definition_digest == published["digest"]
    assert published_record.revision_lineage_json["proposalId"] == proposal[
        "proposalId"
    ]
    assert published_record.revision_lineage_json["humanPatchId"] == human_patch.id
    assert [item["workflowRef"] for item in cards] == [
        published["workflowRef"]
    ]
    assert old_manifest.status == "resolvable"

    for suffix in ("a", "b"):
        _ArticleHandler.content = f"external-{suffix.upper()}"
        run_input = {
            "url": f"{article_site}?version={suffix}",
        }
        async with SessionLocal() as session, session.begin():
            version = await session.get(WorkflowVersion, published["id"])
            run = await Orchestrator().create_run(session, version, run_input)
            run_id = run.id
        for _ in range(5):
            await worker.run_once()
        async with SessionLocal() as session:
            completed = await session.get(Run, run_id)
        assert completed.status == "succeeded"
        assert completed.output_json == {"content": f"external-{suffix.upper()}"}
