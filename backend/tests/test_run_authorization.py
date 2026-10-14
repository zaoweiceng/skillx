import asyncio
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.capabilities import register_skillx
from app.application.compiler import WorkflowCompiler, digest_json
from app.application.orchestrator import Orchestrator
from app.application.run_authorization import approval_plan
from app.config import get_settings
from app.domain.workflow import NodeResult, WorkflowNode
from app.infrastructure.database import (
    Approval,
    NodeRun,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)
from app.runtime.execution_contracts import CancellationToken, RunContext
from app.runtime.executors import SkillXExecutor
from tests.test_capability_locking import skillx_workflow


@pytest.fixture
async def prepared_run(tmp_path, monkeypatch):
    root = tmp_path / "capabilities"
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "capabilities/skillx/text-stats", root / "text-stats"
    )
    monkeypatch.setattr(get_settings(), "capability_root", root)
    async with SessionLocal() as session:
        _, capability = await register_skillx(session, "text-stats")
        manifest = deepcopy(capability.manifest_json)
        manifest["interfaces"]["count"]["effects"] = [
            {"type": "data.write", "confirmation": "required"}
        ]
        manifest["manifest"]["spec"]["interfaces"]["count"]["effects"] = manifest["interfaces"][
            "count"
        ]["effects"]
        capability.manifest_json = manifest
        definition = skillx_workflow(capability.id)
        second = {**definition["spec"]["nodes"][1], "id": "second"}
        definition["spec"]["nodes"].insert(2, second)
        definition["spec"]["edges"].append({"source": "count", "target": "second"})
        workflow = Workflow(id=str(uuid4()), name="approval", draft_json=definition)
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            id=str(uuid4()),
            workflow_id=workflow.id,
            version=1,
            definition_json=definition,
            digest=WorkflowCompiler().compile(definition).digest,
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"text": "hello"})
        await session.commit()
        return run.id, definition, capability.digest


async def test_one_plan_covers_static_nodes_but_not_changed_inputs(prepared_run, monkeypatch):
    run_id, definition, _ = prepared_run
    calls = []

    async def execute(*args, **kwargs):
        calls.append(kwargs["effect_approved"])
        return NodeResult(status="succeeded", output={})

    monkeypatch.setattr("app.application.skillx_execution.execute_skillx", execute)
    executor = SkillXExecutor()

    def context(node_id):
        return RunContext(run_id, {"text": "hello"}, node_id, CancellationToken(asyncio.Event()))

    node = WorkflowNode.model_validate(definition["spec"]["nodes"][1])
    pending = await executor.execute(context("first-attempt"), node, {"text": "hello"})
    snapshot = pending.output["approvalSnapshot"]
    assert snapshot["effect"] == "workflow.run"
    assert len(snapshot["plan"]["operations"]) == 2
    async with SessionLocal() as session:
        session.add(
            NodeRun(
                id="first-attempt",
                run_id=run_id,
                node_id=node.id,
                node_type="skillx",
                status="waiting_user",
                input_json={"text": "hello"},
            )
        )
        await session.flush()
        session.add(
            Approval(
                id=str(uuid4()),
                run_id=run_id,
                node_id=node.id,
                node_run_id="first-attempt",
                status="approved",
                snapshot_json=snapshot,
                snapshot_digest=digest_json(snapshot),
            )
        )
        await session.commit()
    second = WorkflowNode.model_validate(definition["spec"]["nodes"][2])
    assert (
        await executor.execute(context("second-attempt"), second, {"text": "hello"})
    ).status == "succeeded"
    assert calls == [True]
    assert (
        await executor.execute(context("second-attempt"), second, {"text": "changed"})
    ).status == "waiting_user"
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        run.input_json = {"text": "changed"}
        await session.commit()
    assert (
        await executor.execute(context("second-attempt"), second, {"text": "hello"})
    ).status == "waiting_user"


@pytest.mark.parametrize("reason", ["failed", "retry", "cancelled", "source", "version", "dynamic"])
async def test_plan_boundaries(prepared_run, reason):
    run_id, _, _ = prepared_run
    async with SessionLocal() as session:
        before = await approval_plan(session, run_id)
        run = await session.get(Run, run_id)
        if reason == "cancelled":
            from app.infrastructure.database import utcnow

            run.cancellation_requested_at = utcnow()
        elif reason in {"failed", "retry"}:
            node = await session.scalar(
                __import__("sqlalchemy").select(NodeRun).where(NodeRun.run_id == run_id)
            )
            if reason == "failed":
                node.status = "failed"
            else:
                node.attempt = 2
        else:
            version = await session.get(WorkflowVersion, run.workflow_version_id)
            if reason == "source":
                version.digest = "changed"
            elif reason == "version":
                from app.infrastructure.database import CapabilityVersion

                capability = await session.get(
                    CapabilityVersion, before["operations"][0]["operation"]["versionId"]
                )
                capability.digest = "changed"
            else:
                definition = dict(version.definition_json)
                definition["spec"]["nodes"][2]["input"] = {"text": "${count.output.words}"}
                version.definition_json = definition
        await session.flush()
        after = await approval_plan(session, run_id)
        assert before != after
        if reason in {"failed", "retry", "cancelled"}:
            assert after is None
        if reason == "dynamic":
            assert [item["nodeId"] for item in after["operations"]] == ["count"]


@pytest.mark.parametrize("drift", ["missing", "input", "cancelled"])
async def test_approval_transition_rechecks_frozen_plan(prepared_run, drift):
    from fastapi import HTTPException

    from app.api.schemas import Decision
    from app.application.approval_transitions import approve
    from app.infrastructure.database import utcnow

    run_id, _, _ = prepared_run
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node = NodeRun(
            id=str(uuid4()),
            run_id=run_id,
            node_id="count",
            node_type="skillx",
            input_json={"text": "hello"},
            status="waiting_user",
        )
        session.add(node)
        await session.flush()
        plan = await approval_plan(session, run_id)
        operation = plan["operations"][0]
        snapshot = {
            "effect": "workflow.run",
            "plan": plan,
            "operation": operation["operation"],
            "versionDigest": operation["versionDigest"],
        }
        if drift == "missing":
            snapshot.pop("plan")
        approval = Approval(
            id=str(uuid4()),
            run_id=run_id,
            node_run_id=node.id,
            node_id="count",
            status="pending",
            snapshot_json=snapshot,
            snapshot_digest=digest_json(snapshot),
        )
        session.add(approval)
        if drift == "input":
            run.input_json = {"text": "changed"}
        elif drift == "cancelled":
            run.cancellation_requested_at = utcnow()
        await session.flush()
        with pytest.raises(HTTPException) as error:
            await approve(approval.id, Decision(snapshotDigest=approval.snapshot_digest), session)
        assert error.value.status_code == 409
        assert approval.status == "pending"


async def test_host_policy_decision_has_durable_distinct_audit(prepared_run, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from app.api.schemas import Decision
    from app.application.approval_transitions import approve
    from app.infrastructure.database import RunEvent

    run_id, _, _ = prepared_run
    provenance = {"source": "host-policy", "host": "dsh", "sessionId": "session-1"}
    async with SessionLocal() as session:
        node = NodeRun(
            id=str(uuid4()),
            run_id=run_id,
            node_id="count",
            node_type="skillx",
            input_json={"text": "hello"},
            status="waiting_user",
        )
        session.add(node)
        await session.flush()
        plan = await approval_plan(session, run_id)
        item = plan["operations"][0]
        snapshot = {
            "effect": "workflow.run",
            "plan": plan,
            "operation": item["operation"],
            "versionDigest": item["versionDigest"],
        }
        approval = Approval(
            id=str(uuid4()),
            run_id=run_id,
            node_run_id=node.id,
            node_id="count",
            status="pending",
            snapshot_json=snapshot,
            snapshot_digest=digest_json(snapshot),
        )
        session.add(approval)
        await session.flush()
        monkeypatch.setattr(
            "app.application.approval_transitions.orchestrator.advance_run", AsyncMock()
        )
        await approve(
            approval.id,
            Decision(snapshotDigest=approval.snapshot_digest),
            session,
            provenance=provenance,
        )
        assert approval.decided_by == "dsh-host-policy"
        event = await session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "approval.resolved")
        )
        assert event.payload_json["authorization"] == provenance
        assert node.status == "ready"


def test_static_passthrough_propagation_preserves_dynamic_unknowns():
    from app.application.run_authorization import static_passthrough_values
    from app.domain.workflow import WorkflowDefinition

    definition = skillx_workflow("version")
    definition["spec"]["nodes"].insert(
        1,
        {
            "id": "child_input",
            "type": "transform",
            "displayName": "boundary",
            "input": {"text": "${input.text}"},
            "config": {"operation": "passthrough"},
        },
    )
    definition["spec"]["nodes"].insert(
        2,
        {
            "id": "next_input",
            "type": "transform",
            "displayName": "boundary",
            "input": {"text": "${child_input.output.text}"},
            "config": {"operation": "passthrough"},
        },
    )
    definition["spec"]["nodes"].append(
        {
            "id": "unknown",
            "type": "transform",
            "displayName": "dynamic",
            "input": {"value": "${count.output.words}"},
            "config": {"operation": "passthrough"},
        }
    )
    values = static_passthrough_values(
        WorkflowDefinition.model_validate(definition), {"text": "actual"}
    )
    assert values["next_input"] == {"text": "actual"}
    assert "unknown" not in values
    assert "count" not in values


@pytest.mark.parametrize("change", ["none", "upstream", "input", "lock", "cancel", "attempt"])
async def test_runtime_operation_context_is_grounded_in_real_records(prepared_run, change):
    from sqlalchemy import select

    from app.application.run_authorization import operation_context
    from app.infrastructure.database import utcnow

    run_id, definition, version_digest = prepared_run
    version_id = definition["spec"]["nodes"][1]["config"]["capabilityVersionId"]
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        source = await session.get(WorkflowVersion, run.workflow_version_id)
        data = deepcopy(source.definition_json)
        data["spec"]["nodes"][2]["input"] = {"text": "${count.output.text}"}
        source.definition_json = data
        source.validation_json = {
            "capabilityLocks": [
                {
                    "nodeId": "second",
                    "capabilityVersionId": version_id,
                    "interface": "count",
                    "digest": version_digest,
                }
            ]
        }
        upstream = NodeRun(
            id=str(uuid4()),
            run_id=run_id,
            node_id="count",
            node_type="skillx",
            status="succeeded",
            input_json={"text": "hello"},
            output_json={"text": "actual"},
            finished_at=utcnow(),
        )
        current = NodeRun(
            id=str(uuid4()),
            run_id=run_id,
            node_id="second",
            node_type="skillx",
            status="waiting_user",
            input_json={"text": "actual"},
        )
        session.add_all([upstream, current])
        await session.flush()
        input_node = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "input")
        )
        input_node.status = "succeeded"
        input_node.finished_at = utcnow()
        input_node.output_json = {"text": "hello"}
        await session.flush()
        operation = {"versionId": version_id, "interface": "count", "input": {"text": "actual"}}
        context = await operation_context(session, run_id, current.id, operation)
        assert context and context["inputDigest"] == digest_json({"text": "actual"})
        assert context["effects"][0]["type"] == "data.write"
        if change == "upstream":
            upstream.output_json = {"text": "changed"}
        elif change == "input":
            current.input_json = {"text": "changed"}
        elif change == "lock":
            source.validation_json = {"capabilityLocks": []}
        elif change == "cancel":
            run.cancellation_requested_at = utcnow()
        elif change == "attempt":
            session.add(
                NodeRun(
                    id=str(uuid4()),
                    run_id=run_id,
                    node_id="second",
                    node_type="skillx",
                    status="ready",
                    attempt=2,
                    input_json={"text": "actual"},
                )
            )
        await session.flush()
        again = await operation_context(session, run_id, current.id, operation)
        if change == "none":
            assert again == context
        else:
            assert again is None
