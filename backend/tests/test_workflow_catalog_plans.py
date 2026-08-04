from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.application.plans import create_plan_snapshot
from app.application.workflow_catalog import (
    CatalogError,
    build_manifest,
    describe_fragment,
    describe_workflow,
    disclose_refs,
    enforce_session_limit,
    require_disclosed_ref,
    search_manifests,
)
from app.infrastructure.database import (
    PlanSnapshot,
    Run,
    SessionLocal,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowVersion,
)
from tests.fixtures import deterministic_workflow


async def _published(
    name: str = "问候能力",
    *,
    fragment: bool = False,
) -> tuple[Workflow, WorkflowVersion, WorkflowCapabilityManifest]:
    definition = deterministic_workflow()
    definition["metadata"]["capability"] = {
        "summary": "根据姓名生成问候语",
        "useWhen": ["需要问候"],
        "doNotUseWhen": ["需要发送消息"],
        "tags": ["问候", "文本"],
    }
    if fragment:
        definition["metadata"]["callableFragments"] = [
            {
                "name": "format-only",
                "nodeIds": ["format"],
                "summary": "只生成问候文本",
                "outputMapping": {"message": "${format.output.text}"},
            }
        ]
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name=name,
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
            digest=compiled.digest,
            validation_json={"executionClass": "zero_llm", "capabilityLocks": []},
        )
        session.add(version)
        await session.flush()
        manifest = await build_manifest(session, workflow, version)
    return workflow, version, manifest


async def test_catalog_search_is_bounded_and_progressively_disclosed() -> None:
    workflow, version, manifest = await _published(fragment=True)
    async with SessionLocal() as session, session.begin():
        for index in range(999):
            synthetic_workflow = Workflow(
                id=str(uuid4()),
                name=f"批量问候 {index}",
                draft_revision=1,
                draft_json=version.definition_json,
            )
            synthetic_version = WorkflowVersion(
                id=str(uuid4()),
                workflow_id=synthetic_workflow.id,
                version=1,
                definition_json=version.definition_json,
                digest=f"sha256:{index}",
                validation_json={"executionClass": "zero_llm"},
            )
            session.add_all([synthetic_workflow, synthetic_version])
            await session.flush()
            data = {
                **manifest.manifest_json,
                "workflowRef": f"workflow:bulk-{index}@1#sha256:{index}",
                "name": f"批量问候 {index}",
            }
            session.add(
                WorkflowCapabilityManifest(
                    id=str(uuid4()),
                    workflow_version_id=synthetic_version.id,
                    workflow_digest=f"sha256:{index}",
                    manifest_json=data,
                    digest=f"sha256:manifest-{index}",
                    status="published",
                    search_text=f"批量问候 {index} 根据姓名生成问候语",
                )
            )
    async with SessionLocal() as session:
        cards = await search_manifests(session, "问候", limit=8)
        assert len(cards) == 8
        assert len(json.dumps(cards, ensure_ascii=False).encode()) < 24_000
        assert all("stepSummary" not in card and "definition" not in card for card in cards)

        l1 = await describe_workflow(session, manifest.manifest_json["workflowRef"])
        assert l1["versionLock"] == {"versionId": version.id, "digest": version.digest}
        assert "inputSchema" in l1 and "definition" not in l1
        fragment_ref = l1["fragments"][0]["fragmentRef"]
        l2 = await describe_fragment(session, fragment_ref)
        assert l2["sourceWorkflowDigest"] == version.digest
        assert l2["nodeIds"] == ["format"]
        assert l2["sideEffects"] == "none"
        assert workflow.id in l1["workflowRef"]


async def test_two_workflow_refs_compile_to_frozen_plan_and_run() -> None:
    _workflow, version, manifest = await _published()
    ref = manifest.manifest_json["workflowRef"]
    raw_plan = {
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        },
        "outputSchema": {
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string"}},
        },
        "steps": [
            {"id": "first", "kind": "workflow", "ref": ref, "input": {"name": "${input.name}"}},
            {"id": "second", "kind": "workflow", "ref": ref, "input": {"name": "${first.message}"}},
        ],
        "edges": [{"source": "first", "target": "second"}],
        "output": {"message": "${second.message}"},
    }
    async with SessionLocal() as session, session.begin():
        snapshot = await create_plan_snapshot(session, raw_plan)
        run = await Orchestrator().create_plan_run(
            session, snapshot, {"name": "Alice"}
        )
        run_id = run.id
        snapshot_id = snapshot.id

    worker = WorkerService("plan-worker")
    for _ in range(8):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        snapshot = await session.get(PlanSnapshot, snapshot_id)
        assert run is not None and run.status == "succeeded"
        assert run.workflow_version_id is None
        assert run.plan_snapshot_id == snapshot_id
        assert run.output_json == {"message": "你好，你好，Alice"}
        assert snapshot is not None
        assert snapshot.source_refs_json[0]["digest"] == version.digest


def test_catalog_session_limits_and_disclosed_refs() -> None:
    session_id = str(uuid4())
    disclose_refs(session_id, ["workflow:known"])
    require_disclosed_ref(session_id, "workflow:known")
    with pytest.raises(CatalogError):
        require_disclosed_ref(session_id, "workflow:unknown")
    for _ in range(20):
        enforce_session_limit(session_id, "search")
    with pytest.raises(CatalogError):
        enforce_session_limit(session_id, "search")
