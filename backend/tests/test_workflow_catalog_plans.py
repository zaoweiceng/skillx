from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.application.plans import WorkflowSelection, create_plan_snapshot
from app.application.workflow_catalog import (
    CatalogError,
    build_manifest,
    consume_session_call,
    create_disclosure_session,
    describe_fragment,
    describe_workflow,
    disclose_refs,
    require_disclosed_ref,
    resolve_workflow_manifest,
    search_manifests,
)
from app.infrastructure.database import (
    PlanSnapshot,
    Run,
    SessionLocal,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowVersion,
    utcnow,
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


async def test_catalog_session_limits_and_disclosed_refs() -> None:
    async with SessionLocal() as session, session.begin():
        disclosure = await create_disclosure_session(session)
        session_id = disclosure.id
        await disclose_refs(session, session_id, ["workflow:known"], "L0")
        await require_disclosed_ref(session, session_id, "workflow:known")
        with pytest.raises(CatalogError):
            await require_disclosed_ref(session, session_id, "workflow:unknown")
        for _ in range(5):
            await consume_session_call(session, session_id, "search", {})
    async with SessionLocal() as session, session.begin():
        await require_disclosed_ref(session, session_id, "workflow:known")
        for _ in range(15):
            await consume_session_call(session, session_id, "search", {})
        with pytest.raises(CatalogError):
            await consume_session_call(session, session_id, "search", {})
        other = await create_disclosure_session(session)
        with pytest.raises(CatalogError):
            await require_disclosed_ref(
                session, other.id, "workflow:known"
            )


def test_selection_five_actions_are_mutually_exclusive() -> None:
    session_id = str(uuid4())
    assert WorkflowSelection(
        action="ask_user",
        question="缺少什么？",
        missingInputs=["name"],
        sessionId=session_id,
    ).action == "ask_user"
    assert WorkflowSelection(
        action="continue_agent", reason="目录无匹配", sessionId=session_id
    ).action == "continue_agent"
    assert WorkflowSelection(
        action="run_full",
        refs=["workflow:w@1#digest"],
        bindings={},
        sessionId=session_id,
    ).action == "run_full"
    assert WorkflowSelection(
        action="run_fragment",
        refs=["fragment:w@1/part#digest"],
        bindings={},
        sessionId=session_id,
    ).action == "run_fragment"
    assert WorkflowSelection(
        action="compose",
        plan={"steps": []},
        sessionId=session_id,
    ).action == "compose"
    with pytest.raises(ValueError):
        WorkflowSelection(
            action="run_full",
            refs=["fragment:w@1/part#digest"],
            sessionId=session_id,
        )
    with pytest.raises(ValueError):
        WorkflowSelection(
            action="ask_user",
            question="x",
            missingInputs=["name"],
            refs=["workflow:w@1#digest"],
            sessionId=session_id,
        )


async def test_fragment_alias_ports_compile_and_run() -> None:
    definition = deterministic_workflow()
    definition["metadata"]["callableFragments"] = [
        {
            "name": "aliased",
            "nodeIds": ["format"],
            "inputSchema": {
                "type": "object",
                "required": ["person"],
                "properties": {"person": {"type": "string"}},
            },
            "inputMapping": {"name": "${input.person}"},
            "outputSchema": definition["spec"]["outputSchema"],
            "outputMapping": {"message": "${format.output.text}"},
        }
    ]
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()), name="alias", draft_revision=1, draft_json=definition
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=compiled.digest,
            validation_json={"capabilityLocks": []},
        )
        session.add(version)
        await session.flush()
        manifest = await build_manifest(session, workflow, version)
        fragment_ref = manifest.manifest_json["fragments"][0]["fragmentRef"]
        raw_plan = {
            "inputSchema": {
                "type": "object",
                "required": ["who"],
                "properties": {"who": {"type": "string"}},
            },
            "outputSchema": definition["spec"]["outputSchema"],
            "steps": [
                {
                    "id": "part",
                    "kind": "fragment",
                    "ref": fragment_ref,
                    "input": {"person": "${input.who}"},
                }
            ],
            "output": {"message": "${part.message}"},
        }
        snapshot = await create_plan_snapshot(session, raw_plan)
        run = await Orchestrator().create_plan_run(
            session, snapshot, {"who": "Alias"}
        )
        run_id = run.id
        assert snapshot.source_map_json["expandedToSource"]["part__format"][
            "sourceNodeId"
        ] == "format"
    worker = WorkerService("alias-worker")
    for _ in range(6):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded"
        assert run.output_json == {"message": "你好，Alias"}


async def test_expired_plan_rejects_new_run_but_existing_run_resumes() -> None:
    _workflow, _version, manifest = await _published()
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
            {
                "id": "greet",
                "kind": "workflow",
                "ref": ref,
                "input": {"name": "${input.name}"},
            }
        ],
        "output": {"message": "${greet.message}"},
    }
    async with SessionLocal() as session, session.begin():
        snapshot = await create_plan_snapshot(session, raw_plan)
        run = await Orchestrator().create_plan_run(
            session, snapshot, {"name": "Existing"}
        )
        snapshot.expires_at = utcnow() - timedelta(seconds=1)
        snapshot_id = snapshot.id
        run_id = run.id
    async with SessionLocal() as session, session.begin():
        snapshot = await session.get(PlanSnapshot, snapshot_id)
        with pytest.raises(CatalogError):
            await Orchestrator().create_plan_run(
                session, snapshot, {"name": "New"}
            )
    worker = WorkerService("existing-plan-worker")
    for _ in range(6):
        await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "succeeded"


async def test_search_returns_only_active_version_but_old_ref_resolves() -> None:
    workflow, version_one, manifest_one = await _published(name="版本目录")
    old_ref = manifest_one.manifest_json["workflowRef"]
    definition = version_one.definition_json
    async with SessionLocal() as session, session.begin():
        workflow = await session.get(Workflow, workflow.id)
        version_two = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=2,
            definition_json=definition,
            digest=f"{version_one.digest}-v2",
            validation_json=version_one.validation_json,
        )
        session.add(version_two)
        await session.flush()
        manifest_two = await build_manifest(session, workflow, version_two)
        new_ref = manifest_two.manifest_json["workflowRef"]
    async with SessionLocal() as session:
        cards = await search_manifests(session, "版本目录", limit=8)
        refs = {item["workflowRef"] for item in cards}
        assert new_ref in refs
        assert old_ref not in refs
        resolved = await resolve_workflow_manifest(session, old_ref)
        assert resolved.workflow_version_id == version_one.id
