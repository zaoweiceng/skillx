import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.api.routes import Decision, approve
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import Orchestrator, WorkerService
from app.infrastructure.database import (
    Approval,
    Artifact,
    NodeRun,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


class FormSiteHandler(BaseHTTPRequestHandler):
    extra_required = False
    unknown_result = False

    def do_GET(self) -> None:
        if self.path != "/apply":
            self.send_error(404)
            return
        extra = (
            '<label>成本中心<input data-contract-id="cost-center" '
            'name="cost-center" required></label>'
            if self.extra_required
            else ""
        )
        success_script = (
            ""
            if self.unknown_result
            else """
                document.querySelector('#success').hidden = false;
                document.querySelector('#business-id').textContent = 'EXP-PLAYWRIGHT-001';
            """
        )
        body = f"""
<!doctype html>
<html>
  <head><title>费用申请</title></head>
  <body>
    <div id="identity">user-1</div>
    <div id="business-object">expense</div>
    <form>
      <label>金额<input data-contract-id="amount" id="amount" name="amount"
        type="number" required></label>
      {extra}
      <button type="button" id="submit">提交申请</button>
    </form>
    <div id="success" hidden>提交成功 <span id="business-id"></span></div>
    <script>
      document.querySelector('#submit').addEventListener('click', () => {{
        {success_script}
      }});
    </script>
  </body>
</html>
"""
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def form_site() -> Iterator[tuple[str, type[FormSiteHandler]]]:
    FormSiteHandler.extra_required = False
    FormSiteHandler.unknown_result = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FormSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/apply", FormSiteHandler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def playwright_workflow(url: str) -> dict[str, Any]:
    contract = {
        "contractVersion": "1",
        "urlPattern": url,
        "pageIdentity": {
            "title": "费用申请",
            "identity": "user-1",
            "businessObject": "expense",
        },
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
        "metadata": {"name": "playwright-submit"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"amount": {"type": "number"}},
                "required": ["amount"],
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
                    "displayName": "提交费用",
                    "input": {"formValues": {"amount": "${input.amount}"}},
                    "config": {
                        "runtime": "playwright",
                        "url": url,
                        "pageContract": contract,
                        "identitySelector": "#identity",
                        "businessObjectSelector": "#business-object",
                        "submit": True,
                        "successAssertions": [
                            {"type": "text", "value": "提交成功", "timeoutMs": 500}
                        ],
                        "resultSelectors": {"businessId": "#business-id"},
                    },
                    "sideEffect": True,
                    "idempotent": False,
                    "timeoutSeconds": 20,
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


async def create_run(url: str) -> str:
    definition = playwright_workflow(url)
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="playwright-submit",
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
            validation_json={"executionClass": "zero_llm"},
        )
        session.add(version)
        await session.flush()
        run = await Orchestrator().create_run(session, version, {"amount": 128.5})
        return run.id


async def pending_approval(run_id: str) -> Approval:
    async with SessionLocal() as session:
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.status == "pending",
            )
        )
        assert approval is not None
        return approval


async def approve_browser(run_id: str) -> None:
    approval = await pending_approval(run_id)
    async with SessionLocal() as session:
        await approve(
            approval.id,
            Decision(snapshotDigest=approval.snapshot_digest),
            session,
        )


async def test_real_playwright_submit_requires_replay_and_saves_evidence(
    form_site: tuple[str, type[FormSiteHandler]],
) -> None:
    url, _ = form_site
    run_id = await create_run(url)
    worker = WorkerService("playwright-worker")
    assert await worker.run_once()
    assert await worker.run_once()
    await approve_browser(run_id)
    assert await worker.run_once()
    assert await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        artifacts = (
            await session.scalars(select(Artifact).where(Artifact.run_id == run_id))
        ).all()
        assert run is not None and run.status == "succeeded"
        assert run.output_json["businessId"] == "EXP-PLAYWRIGHT-001"
        assert {item.kind for item in artifacts} >= {
            "browser-dom-summary",
            "browser-filled",
            "browser-submitted",
        }


async def test_real_playwright_blocks_new_required_field_after_approval(
    form_site: tuple[str, type[FormSiteHandler]],
) -> None:
    url, handler = form_site
    run_id = await create_run(url)
    worker = WorkerService("playwright-worker")
    await worker.run_once()
    await worker.run_once()
    await approve_browser(run_id)
    handler.extra_required = True
    await worker.run_once()

    async with SessionLocal() as session:
        node_run = await session.scalar(
            select(NodeRun).where(
                NodeRun.run_id == run_id,
                NodeRun.node_id == "submit",
            )
        )
        assert node_run is not None
        assert node_run.status == "failed"
        assert node_run.error_json["code"] == "PAGE_DRIFT"
        assert node_run.error_json["details"]["level"] == "L3"


async def test_real_playwright_unknown_submit_result_is_not_retried(
    form_site: tuple[str, type[FormSiteHandler]],
) -> None:
    url, handler = form_site
    handler.unknown_result = True
    run_id = await create_run(url)
    worker = WorkerService("playwright-worker")
    await worker.run_once()
    await worker.run_once()
    await approve_browser(run_id)
    await worker.run_once()

    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        node_runs = (
            await session.scalars(
                select(NodeRun).where(
                    NodeRun.run_id == run_id,
                    NodeRun.node_id == "submit",
                )
            )
        ).all()
        assert run is not None and run.status == "waiting_approval"
        assert len(node_runs) == 1
        assert node_runs[0].error_json["code"] == "UNKNOWN_OUTCOME"
