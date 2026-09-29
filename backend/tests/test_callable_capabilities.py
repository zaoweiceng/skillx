import asyncio
import json
import shutil
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.tasks import (
    InvocationCreate,
    cancel_invocation,
    create_invocation,
    invocation_stream,
    invocations,
)
from app.application.agent_host import AgentHost, AgentHostResult
from app.application.callable_capabilities import callable_catalog, resolve_callable
from app.application.capabilities import register_skillx
from app.application.compiler import WorkflowCompiler
from app.application.invocations import InvocationDriver, enqueue_invocation
from app.application.learning_validation import validate_candidate
from app.application.orchestrator import Orchestrator, WorkerService
from app.application.task_contracts import TaskDecision
from app.application.task_learning import ContractExample
from app.application.tasks import TaskDriver, finish_task, task_catalog
from app.application.workflow_nesting import expand_workflow
from app.config import get_settings
from app.infrastructure.database import Run, SessionLocal
from app.infrastructure.models.tasks import Invocation, LearningJob, Task
from tests.test_tasks import make_task
from tests.test_workflow_catalog_plans import _published


async def skill_version(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "capability_root", tmp_path)
    source = Path(__file__).resolve().parents[2] / "capabilities/skillx/text-stats"
    shutil.copytree(source, tmp_path / "text-stats")
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(session, "text-stats")
    return version


@pytest.mark.parametrize("implementation", ["workflow", "skillx"])
async def test_same_public_contract_dispatches_and_persists_result(
    implementation, tmp_path, monkeypatch
):
    if implementation == "workflow":
        _, version, _ = await _published()
        interface, inputs, expected = "execute", {"name": "Ada"}, {"message": "你好，Ada"}
    else:
        version = await skill_version(tmp_path, monkeypatch)
        interface, inputs = "count", {"text": "Hello world"}
        expected = {"characters": 11, "nonWhitespaceCharacters": 10, "words": 2}
    async with SessionLocal() as session:
        contract = (await resolve_callable(session, version.id)).contract
        assert set(contract["interfaces"][interface]) >= {"inputSchema", "outputSchema"}
        assert "kind" not in contract["interfaces"][interface]
        assert "runtime" not in contract["interfaces"][interface]
        body = InvocationCreate(interface=interface, input=inputs, idempotencyKey="same-operation")
        result = await create_invocation(version.id, body, session)
        assert result["kind"] == "capability"
        assert result["request"] == {
            "capabilityVersionId": version.id,
            "interface": interface,
            "input": inputs,
        }
        duplicate = await enqueue_invocation(
            session,
            kind=implementation,
            arguments={
                "versionId": version.id,
                "input": inputs,
                **({"interface": interface} if implementation == "skillx" else {}),
            },
            key="same-operation",
        )
        assert duplicate.id == result["id"]
        with pytest.raises(HTTPException) as invalid:
            await create_invocation(
                version.id, InvocationCreate(interface=interface, input={}), session
            )
        assert invalid.value.status_code == 422
    worker = WorkerService("unified-test")
    for _ in range(8):
        await InvocationDriver().poll_once()
        await worker.run_once()
    async with SessionLocal() as session:
        history = await invocations(version.id, session)
        assert len(history) == 1
        assert history[0]["status"] == "succeeded", history
        assert history[0]["output"] == expected
        assert (await session.get(Task, result["taskId"])).decisions == 0


async def test_catalog_and_outer_agent_never_need_implementation(tmp_path, monkeypatch):
    _, workflow, _ = await _published()
    skill = await skill_version(tmp_path, monkeypatch)
    task_id = await make_task()
    async with SessionLocal() as session:
        catalog = await task_catalog(session)
        assert set(catalog) == {"capabilities", "tools"}
        assert {x["id"] for x in catalog["capabilities"]} == {workflow.id, skill.id}
        assert all("stepSummary" not in x for x in catalog["capabilities"])
    schema = TaskDecision.model_json_schema()
    assert schema["$defs"]["TaskCall"]["properties"]["kind"]["enum"] == [
        "capability",
        "tool",
        "code",
        "install",
    ]

    async def decide(self, request, **kwargs):
        assert '"capabilities"' in request.prompt
        assert '"workflows"' not in request.prompt
        return AgentHostResult(
            output={
                "action": "dispatch",
                "summary": "调用能力",
                "completionCriteria": ["完成问候"],
                "calls": [
                    {
                        "key": "greet",
                        "kind": "capability",
                        "reason": "满足目标",
                        "arguments": {
                            "capabilityVersionId": workflow.id,
                            "interface": "execute",
                            "input": {"name": "Ada"},
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(AgentHost, "execute", decide)
    await TaskDriver().poll_once()
    async with SessionLocal() as session:
        call = await session.scalar(select(Invocation).where(Invocation.task_id == task_id))
        assert call.kind == "workflow"  # Host-only executor binding.
        assert call.request_json["versionId"] == workflow.id

    worker = WorkerService("outer-unified-test")
    for _ in range(8):
        await InvocationDriver().poll_once()
        await worker.run_once()

    async def complete(self, request, **kwargs):
        context = json.loads(request.prompt.rsplit("\n", 1)[1])
        observed = context["calls"][0]
        assert observed["kind"] == "capability"
        assert "versionId" not in observed["request"]
        assert observed["output"] == {"message": "你好，Ada"}
        return AgentHostResult(
            output={
                "action": "complete",
                "summary": "完成",
                "completionCriteria": ["完成问候"],
                "evidence": [observed["id"]],
                "answer": "你好，Ada",
            }
        )

    monkeypatch.setattr(AgentHost, "execute", complete)
    await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, task_id)).status == "succeeded"
        assert not await session.scalar(select(LearningJob.id))


def composed(version, interface, schema, output_schema, inputs, outputs):
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "parent"},
        "spec": {
            "inputSchema": schema,
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "call",
                    "type": "capability_call",
                    "displayName": "调用能力",
                    "config": {
                        "capabilityVersionId": version.id,
                        "interface": interface,
                        "digest": version.digest,
                    },
                    "input": inputs,
                },
                {"id": "output", "type": "output", "displayName": "输出", "input": outputs},
            ],
            "edges": [
                {"source": "input", "target": "call"},
                {"source": "call", "target": "output"},
            ],
        },
    }


@pytest.mark.parametrize("implementation", ["workflow", "skillx"])
async def test_same_capability_node_expands_and_validates_real_graph(
    implementation, tmp_path, monkeypatch
):
    if implementation == "workflow":
        _, version, _ = await _published()
        interface, inputs, expected = "execute", {"name": "Ada"}, {"message": "你好，Ada"}
        second, second_expected = {"name": "Lin"}, {"message": "你好，Lin"}
    else:
        version = await skill_version(tmp_path, monkeypatch)
        interface, inputs = "count", {"text": "Hello world"}
        expected = {"characters": 11, "nonWhitespaceCharacters": 10, "words": 2}
        second, second_expected = (
            {"text": "a"},
            {"characters": 1, "nonWhitespaceCharacters": 1, "words": 1},
        )
    async with SessionLocal() as session:
        contract = (await resolve_callable(session, version.id)).contract["interfaces"][interface]
        definition = composed(
            version,
            interface,
            contract["inputSchema"],
            contract["outputSchema"],
            {k: "${input." + k + "}" for k in inputs},
            {k: "${call.output." + k + "}" for k in expected},
        )
        WorkflowCompiler().compile(definition)
        expanded = await expand_workflow(session, definition)
        assert not any(
            n["type"] in {"capability_call", "workflow_call"} for n in expanded["spec"]["nodes"]
        )
        with pytest.raises(ValueError, match="digest"):
            wrong = deepcopy(definition)
            wrong["spec"]["nodes"][1]["config"]["digest"] = "wrong"
            await expand_workflow(session, wrong)
        witness = Invocation(id="evidence", status="succeeded", output_json=expected)
        await validate_candidate(
            session,
            definition,
            [
                ContractExample(input=inputs, expected=expected, sourceInvocationId="evidence"),
                ContractExample(input=second, expected=second_expected),
            ],
            {"evidence": witness},
        )


async def test_cancel_unified_invocation_cancels_underlying_run():
    _, version, _ = await _published()
    async with SessionLocal() as session:
        item = await create_invocation(version.id, InvocationCreate(input={"name": "Ada"}), session)
    await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        call = await session.get(Invocation, item["id"])
        assert call.run_id
        result = await cancel_invocation(call.id, session)
        assert result["status"] == "cancelled"
        run = await session.get(Run, call.run_id)
        assert run.status == "cancelled"


async def test_workflow_start_error_is_a_terminal_invocation(monkeypatch):
    _, version, _ = await _published()
    async with SessionLocal() as session:
        item = await create_invocation(version.id, InvocationCreate(input={"name": "Ada"}), session)

    async def fail(*args, **kwargs):
        raise ValueError("locked dependency unavailable")

    monkeypatch.setattr(Orchestrator, "create_run", fail)
    await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        call = await session.get(Invocation, item["id"])
        assert call.status == "failed"
        assert call.error_json["code"] == "CAPABILITY_START_FAILED"
        assert call.run_id is None


@pytest.mark.parametrize(
    "kinds,learn",
    [
        (["skillx"], False),
        (["workflow"], False),
        (["workflow", "workflow"], True),
        (["code"], True),
    ],
)
async def test_learning_trigger_uses_composition_not_implementation(kinds, learn):
    task_id = await make_task()
    async with SessionLocal() as session, session.begin():
        task = await session.get(Task, task_id)
        for index, kind in enumerate(kinds):
            session.add(
                Invocation(
                    id=str(uuid4()),
                    task_id=task_id,
                    scope=task_id,
                    call_key=str(index),
                    kind=kind,
                    status="succeeded",
                )
            )
        await session.flush()
        await finish_task(session, task, "succeeded", "完成")
        await session.flush()
        assert bool(await session.scalar(select(LearningJob.id))) == learn


async def test_old_pinned_version_stays_resolvable_but_not_in_catalog():
    from app.infrastructure.database import WorkflowCapabilityManifest

    _, version, manifest = await _published()
    async with SessionLocal() as session, session.begin():
        stored = await session.get(WorkflowCapabilityManifest, manifest.id)
        stored.status = "resolvable"
        await session.flush()
        assert not await callable_catalog(session)
        assert (await resolve_callable(session, version.id)).contract["id"] == version.id
        assert "workflow" not in json.dumps((await resolve_callable(session, version.id)).contract)


async def test_workflow_approval_is_visible_and_cancelled_through_same_api():
    from app.infrastructure.database import WorkflowVersion
    from tests.fixtures import deterministic_workflow

    _, version, _ = await _published()
    async with SessionLocal() as session:
        stored = await session.get(WorkflowVersion, version.id)
        stored.definition_json = deterministic_workflow(with_approval=True)
        stored.digest = WorkflowCompiler().compile(stored.definition_json).digest
        await session.commit()
        contract = (await resolve_callable(session, version.id)).contract
        assert contract["interfaces"]["execute"]["effects"][0]["confirmation"] == "required"
        assert contract["interfaces"]["execute"]["execution"]["idempotency"] == "none"
        created = await create_invocation(
            version.id, InvocationCreate(input={"name": "Ada"}), session
        )
    worker = WorkerService("approval-unified-test")
    for _ in range(8):
        await InvocationDriver().poll_once()
        await worker.run_once()
    async with SessionLocal() as session:
        history = await invocations(version.id, session)
        assert history[0]["interactions"][0]["status"] == "pending"
        await cancel_invocation(created["id"], session)
        history = await invocations(version.id, session)
        assert history[0]["interactions"][0]["status"] == "cancelled"


def test_error_contract_does_not_depend_on_executor():
    from app.application.callable_capabilities import public_error

    expected = {"code": "TIMEOUT", "message": "执行超时", "retryable": True, "details": {}}
    assert public_error({"code": "TIMEOUT", "message": "执行超时", "retryable": True}) == expected
    assert (
        public_error(
            {
                "code": "TIMEOUT",
                "userMessage": "执行超时",
                "retryable": True,
                "technicalDetail": {"stack": "internal"},
            }
        )
        == expected
    )


async def test_manual_invocation_event_stream_ends_without_outer_task_completion():
    from types import SimpleNamespace

    _, version, _ = await _published()
    async with SessionLocal() as session:
        item = await create_invocation(version.id, InvocationCreate(input={"name": "Ada"}), session)
        await cancel_invocation(item["id"], session)

    async def disconnected():
        return False

    request = SimpleNamespace(headers={}, is_disconnected=disconnected)
    response = await invocation_stream(item["id"], request)
    async with asyncio.timeout(2):
        chunks = [chunk async for chunk in response.body_iterator]
    assert any('"kind": "capability"' in chunk for chunk in chunks)


async def test_ui_schema_is_snapshotted_and_cannot_escape_package(tmp_path, monkeypatch):
    import yaml

    from app.runtime.skillx import SkillXValidationError, load_manifest

    await skill_version(tmp_path, monkeypatch)
    package = tmp_path / "text-stats"
    path = package / "skillx.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["metadata"]["version"] = "1.0.1"
    manifest["spec"]["interfaces"]["count"]["uiSchema"] = "ui.json"
    path.write_text(yaml.safe_dump(manifest))
    (package / "ui.json").write_text('{"text":{"ui:widget":"textarea"}}')
    async with SessionLocal() as session, session.begin():
        _, version = await register_skillx(session, "text-stats")
        contract = (await resolve_callable(session, version.id)).contract
        assert contract["interfaces"]["count"]["uiSchema"] == {"text": {"ui:widget": "textarea"}}
    (tmp_path / "outside.json").write_text("{}")
    manifest["spec"]["interfaces"]["count"]["uiSchema"] = "../outside.json"
    path.write_text(yaml.safe_dump(manifest))
    with pytest.raises(SkillXValidationError, match="包内路径"):
        load_manifest(package)
