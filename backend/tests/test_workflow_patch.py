"""005-E：失败可复现性与受控 WorkflowPatch 测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.routes import apply_version_workflow_patch
from app.application.workflow_patch import (
    WorkflowPatch,
    WorkflowPatchViolation,
    apply_workflow_patch,
    classify_failure,
    failure_fingerprint,
    should_propose_patch,
)
from app.infrastructure.database import SessionLocal, Workflow, WorkflowVersion


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


async def test_workflow_patch_endpoint_creates_draft_without_touching_version() -> None:
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
        version_id = version.id
        original = dict(version.definition_json)

    async with SessionLocal() as session:
        result = await apply_version_workflow_patch(
            version_id,
            {"nodePatches": [{"nodeId": "work", "changes": {"timeoutSeconds": 90}}]},
            session,
        )
    assert result["draftWorkflowId"]
    assert result["baseVersionId"] == version_id
    # 旧版本定义不变。
    async with SessionLocal() as session:
        version = await session.get(WorkflowVersion, version_id)
        assert version.definition_json == original
        work = next(n for n in version.definition_json["spec"]["nodes"] if n["id"] == "work")
        assert work["timeoutSeconds"] == 30


async def test_concurrent_patch_applications_create_independent_drafts() -> None:
    """并发对同一已发布版本应用不同补丁：各自生成独立 draft，基线版本始终不变。"""

    import asyncio

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
        version_id = version.id
        original = dict(version.definition_json)

    async def apply_one(timeout_seconds: int) -> dict:
        # 每个并发应用使用独立 Session，模拟两个请求同时打到同一基线版本。
        async with SessionLocal() as session:
            return await apply_version_workflow_patch(
                version_id,
                {
                    "nodePatches": [
                        {"nodeId": "work", "changes": {"timeoutSeconds": timeout_seconds}}
                    ]
                },
                session,
            )

    results = await asyncio.gather(apply_one(60), apply_one(120))
    draft_ids = {result["draftWorkflowId"] for result in results}
    # 两次应用生成两个互不相同的 draft，互不覆盖。
    assert len(draft_ids) == 2
    # 基线版本在并发应用后仍保持只读、原样不变。
    async with SessionLocal() as session:
        version = await session.get(WorkflowVersion, version_id)
        assert version.definition_json == original
        # 两个 draft 各自带着自己的 timeout。
        timeouts = set()
        for result in results:
            draft = await session.get(Workflow, result["draftWorkflowId"])
            work = next(n for n in draft.draft_json["spec"]["nodes"] if n["id"] == "work")
            timeouts.add(work["timeoutSeconds"])
        assert timeouts == {60, 120}
