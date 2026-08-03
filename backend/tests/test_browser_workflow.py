from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.api.routes import Decision, approve
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
