"""005-E/006-D：失败可复现性、受控 WorkflowPatch DSL、后端 eligibility 与真实 replay。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.routes import apply_version_workflow_patch
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
from app.infrastructure.database import (
    FailureObservation,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
    utcnow,
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
    # 仅出现一次：未达可复现阈值 → 不 eligible。
    assert observation_eligibility(obs)["eligible"] is False
    obs.occurrence = REPRODUCIBLE_THRESHOLD
    result = observation_eligibility(obs)
    assert result["eligible"] is True
    assert result["reproducible"] is True


async def _seed_version_with_failure(
    *, error_code: str, occurrence: int, node_id: str = "work"
) -> tuple[str, str]:
    """建版本 + source Run + 指定 occurrence 的 FailureObservation。返回 (version_id, run_id)。"""

    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="wf", draft_revision=1, draft_json=_definition())
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=_definition(),
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
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "PATCH_NOT_ELIGIBLE"


async def test_patch_endpoint_rejects_transient_single_occurrence() -> None:
    # 一次 TIMEOUT（瞬态、未达阈值）→ 不 eligible。
    version_id, run_id = await _seed_version_with_failure(
        error_code="TIMEOUT", occurrence=1
    )
    async with SessionLocal() as session:
        with pytest.raises(HTTPException) as excinfo:
            await apply_version_workflow_patch(
                version_id,
                {
                    "sourceRunId": run_id,
                    "patch": {
                        "nodePatches": [{"nodeId": "work", "changes": {"timeoutSeconds": 90}}]
                    },
                },
                session,
            )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["code"] == "PATCH_NOT_ELIGIBLE"


async def test_patch_endpoint_creates_draft_after_real_replay() -> None:
    # 可复现的确定性失败 + 真实 replay 通过 → 创建 draft，旧版本不变。
    version_id, run_id = await _seed_version_with_failure(
        error_code="INVALID_INPUT", occurrence=REPRODUCIBLE_THRESHOLD
    )
    async with SessionLocal() as session:
        original = (await session.get(WorkflowVersion, version_id)).definition_json
        result = await apply_version_workflow_patch(
            version_id,
            {
                "sourceRunId": run_id,
                "patch": {
                    "nodePatches": [{"nodeId": "work", "changes": {"timeoutSeconds": 90}}]
                },
            },
            session,
        )
    assert result["draftWorkflowId"]
    assert result["replay"]["passed"] is True
    assert result["eligibility"]["eligible"] is True
    async with SessionLocal() as session:
        version = await session.get(WorkflowVersion, version_id)
        assert version.definition_json == original
        work = next(n for n in version.definition_json["spec"]["nodes"] if n["id"] == "work")
        assert work["timeoutSeconds"] == 30
