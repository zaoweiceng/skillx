from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.api.routes import Decision, approve, reject
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import (
    Approval,
    NodeRun,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


def browser_workflow() -> dict[str, Any]:
    contract = {
        "contractVersion": "1",
        "urlPattern": "https://forms.example.org/apply*",
        "pageIdentity": {"identity": "user-1", "businessObject": "expense"},
        "fields": [
            {
                "id": "amount",
                "required": True,
                "type": "number",
                "locator": {"role": "spinbutton", "label": "金额"},
            }
        ],
        "submit": {"role": "button", "name": "提交申请"},
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "browser-submit"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "observedPage": {"type": "object"},
                    "formValues": {"type": "object"},
                },
                "required": ["observedPage", "formValues"],
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "businessId": {"type": "string"},
                    "evidence": {"type": "object"},
                },
                "required": ["status", "businessId", "evidence"],
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "submit",
                    "type": "browser",
                    "displayName": "提交表单",
                    "input": {
                        "observedPage": "${input.observedPage}",
                        "formValues": "${input.formValues}",
                    },
                    "config": {
                        "pageContract": contract,
                        "submit": True,
                        "fakeSubmitResult": {
                            "status": "succeeded",
                            "businessId": "EXP-001",
                        },
                    },
                    "sideEffect": True,
                    "idempotent": False,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {
                        "status": "${submit.output.status}",
                        "businessId": "${submit.output.businessId}",
                        "evidence": "${submit.output.evidence}",
                    },
                },
            ],
            "edges": [
                {"source": "input", "target": "submit"},
                {"source": "submit", "target": "output"},
            ],
        },
    }


def inputs() -> dict[str, Any]:
    return {
        "observedPage": {
            "url": "https://forms.example.org/apply",
            "identity": "user-1",
            "businessObject": "expense",
            "fingerprint": "page-v1",
            "fields": [
                {
                    "id": "amount",
                    "required": True,
                    "type": "number",
                    "locator": {"role": "spinbutton", "label": "金额"},
                }
            ],
            "submit": {"role": "button", "name": "提交申请"},
        },
        "formValues": {"amount": 100},
    }


async def create_browser_run() -> str:
    definition = browser_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="browser-submit",
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
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, inputs())
        return run.id


async def approve_pending(run_id: str) -> None:
    async with SessionLocal() as session:
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.status == "pending",
            )
        )
        assert approval is not None
        await approve(
            approval.id,
            Decision(snapshotDigest=approval.snapshot_digest),
            session,
        )


async def test_browser_submit_requires_and_consumes_one_time_approval() -> None:
    run_id = await create_browser_run()
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.status == "waiting_approval"

    await approve_pending(run_id)
    await worker.run_once()
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        approval = await session.scalar(select(Approval).where(Approval.run_id == run_id))
        assert run is not None and run.status == "succeeded"
        assert run.output_json["businessId"] == "EXP-001"
        assert approval is not None and approval.status == "approved"


async def test_browser_authorization_invalidates_when_form_changes() -> None:
    run_id = await create_browser_run()
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()
    await approve_pending(run_id)
    async with SessionLocal() as session, session.begin():
        node_run = await session.scalar(
            select(NodeRun).where(
                NodeRun.run_id == run_id,
                NodeRun.node_id == "submit",
            )
        )
        assert node_run is not None
        node_run.input_json = {
            **node_run.input_json,
            "formValues": {"amount": 101},
        }

    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(
                NodeRun.run_id == run_id,
                NodeRun.node_id == "submit",
            )
        )
        assert run is not None and run.status == "waiting_approval"
        assert node_run is not None
        assert node_run.error_json["code"] == "CONFIRMATION_REQUIRED"


def _drift_observed(*, extra_optional: str | None = "note") -> dict[str, Any]:
    """构造与契约仅存在可解释差异（新增可选字段）的观察页面，触发 L2。"""

    fields: list[dict[str, Any]] = [
        {
            "id": "amount",
            "required": True,
            "type": "number",
            "locator": {"role": "spinbutton", "label": "金额"},
        }
    ]
    if extra_optional is not None:
        fields.append(
            {
                "id": extra_optional,
                "required": False,
                "type": "text",
                "locator": {"role": "textbox", "label": extra_optional},
            }
        )
    return {
        "url": "https://forms.example.org/apply",
        "identity": "user-1",
        "businessObject": "expense",
        "fingerprint": "page-v1",
        "fields": fields,
        "submit": {"role": "button", "name": "提交申请"},
    }


def drift_workflow(*, submit: bool) -> dict[str, Any]:
    definition = browser_workflow()
    node = next(item for item in definition["spec"]["nodes"] if item["id"] == "submit")
    node["config"]["submit"] = submit
    if not submit:
        node["config"].pop("fakeSubmitResult", None)
        node["sideEffect"] = False
        node["idempotent"] = True
        # 只读节点输出 drift/formValues，需要放宽输出契约。
        definition["spec"]["outputSchema"] = {"type": "object"}
        output_node = next(
            item for item in definition["spec"]["nodes"] if item["id"] == "output"
        )
        output_node["input"] = {"drift": "${submit.output.drift}"}
    return definition


async def create_drift_run(*, submit: bool, observed: dict[str, Any]) -> str:
    definition = drift_workflow(submit=submit)
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="browser-drift",
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
            validation_json={},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(
            session,
            version,
            {"observedPage": observed, "formValues": {"amount": 100}},
        )
        return run.id


async def pending_effect(run_id: str) -> str:
    async with SessionLocal() as session:
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.status == "pending",
            )
        )
        assert approval is not None
        return approval.snapshot_json.get("effect", "")


async def test_browser_l2_readonly_requires_drift_confirmation_then_succeeds() -> None:
    run_id = await create_drift_run(submit=False, observed=_drift_observed())
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert run is not None and run.status == "waiting_approval"
        assert node_run is not None and node_run.error_json["code"] == "PAGE_DRIFT"
        assert node_run.error_json["details"]["level"] == "L2"
    assert await pending_effect(run_id) == "browser.drift"

    await approve_pending(run_id)
    await worker.run_once()
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert run is not None and run.status == "succeeded"
        assert node_run is not None and node_run.status == "succeeded"


async def test_browser_l2_submit_still_requires_submit_confirmation() -> None:
    run_id = await create_drift_run(submit=True, observed=_drift_observed())
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()

    # 第一次暂停必须是漂移确认，绝不能直接成功或直接提交。
    assert await pending_effect(run_id) == "browser.drift"
    await approve_pending(run_id)
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert run is not None and run.status == "waiting_approval"
        assert node_run is not None and node_run.status == "waiting_user"
        # 漂移批准后仍进入提交确认，尚未提交成功。
        assert node_run.output_json.get("businessId") is None
    assert await pending_effect(run_id) == "form.submit"

    await approve_pending(run_id)
    await worker.run_once()
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.status == "succeeded"
        assert run.output_json["businessId"] == "EXP-001"


async def test_browser_l2_second_drift_invalidates_prior_authorization() -> None:
    run_id = await create_drift_run(submit=False, observed=_drift_observed())
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()
    await approve_pending(run_id)

    # 授权后页面再次变化（不同的可选字段），旧漂移授权应失效。
    async with SessionLocal() as session, session.begin():
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert node_run is not None
        node_run.input_json = {
            **node_run.input_json,
            "observedPage": _drift_observed(extra_optional="memo"),
        }

    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert run is not None and run.status == "waiting_approval"
        assert node_run is not None and node_run.status == "waiting_user"
        assert node_run.error_json["code"] == "PAGE_DRIFT"


async def test_browser_l2_rejection_fails_node() -> None:
    run_id = await create_drift_run(submit=True, observed=_drift_observed())
    worker = WorkerService("browser-worker")
    await worker.run_once()
    await worker.run_once()
    assert await pending_effect(run_id) == "browser.drift"

    async with SessionLocal() as session:
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.status == "pending",
            )
        )
        assert approval is not None
        await reject(approval.id, session)

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_run = await session.scalar(
            select(NodeRun).where(NodeRun.run_id == run_id, NodeRun.node_id == "submit")
        )
        assert node_run is not None and node_run.status == "failed"
        assert run is not None and run.status == "failed"
