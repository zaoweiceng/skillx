import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.api.routes import get_run
from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from app.application.orchestrator import Orchestrator, WorkerService
from app.config import get_settings
from app.infrastructure.database import (
    ModelUsage,
    Run,
    RunEvent,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)
from app.runtime.models import ModelRuntimeError, OpenAICompatibleAdapter

STUB = Path(__file__).resolve().parents[2] / "examples" / "traex_stub.py"


class _ModelHandler(BaseHTTPRequestHandler):
    status = 200

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        if self.status != 200:
            self.send_response(self.status)
            self.end_headers()
            return
        payload = {
            "model": "direct-test-model",
            "choices": [{"message": {"content": json.dumps({"category": "direct"})}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


class ModelServer:
    def __init__(self, status: int) -> None:
        handler = type("ConfiguredModelHandler", (_ModelHandler,), {"status": status})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1/chat/completions"

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def model_workflow(node_type: str = "llm") -> dict[str, Any]:
    output_schema = {
        "type": "object",
        "properties": {"category": {"type": "string"}},
        "required": ["category"],
        "additionalProperties": False,
    }
    config: dict[str, Any] = {
        "outputSchema": output_schema,
        "fakeOutput": {"category": "feedback"},
        "prompt": "分类：{input}",
    }
    if node_type == "llm":
        config["modelProfile"] = "fake"
    else:
        config.update(
            agentProfile="fake",
            maxSteps=3,
            maxTotalTokens=100,
            allowedCapabilities=[],
        )
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": f"{node_type}-demo"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "outputSchema": output_schema,
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "semantic",
                    "type": node_type,
                    "displayName": "语义节点",
                    "input": {"text": "${input.text}"},
                    "config": config,
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"category": "${semantic.output.category}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "semantic"},
                {"source": "semantic", "target": "output"},
            ],
        },
    }


async def execute_workflow(definition: dict[str, Any]) -> tuple[str, Run]:
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="model-demo",
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
        run = await Orchestrator().create_run(session, version, {"text": "产品很好"})
        run_id = run.id
    worker = WorkerService("model-worker")
    for _ in range(5):
        if not await worker.run_once():
            break
    async with SessionLocal() as session:
        completed = await session.get(Run, run_id)
        assert completed is not None
        return run_id, completed


async def test_hybrid_workflow_only_llm_node_reports_tokens() -> None:
    run_id, run = await execute_workflow(model_workflow("llm"))

    assert run.status == "succeeded"
    assert run.output_json == {"category": "feedback"}
    async with SessionLocal() as session:
        events = (
            await session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run_id,
                    RunEvent.type == "llm.call.completed",
                )
            )
        ).all()
        usage = await session.scalar(
            select(ModelUsage).where(ModelUsage.run_id == run_id)
        )
        detail = await get_run(run_id, session)
        assert len(events) == 1
        assert events[0].payload_json["inputTokens"] > 0
        assert usage is not None
        assert usage.request_dispatched_at is None
        assert detail["runtimeSnapshot"]["modelCalled"] is False


async def test_fake_agent_emits_auditable_events() -> None:
    run_id, run = await execute_workflow(model_workflow("agent"))

    assert run.status == "succeeded"
    async with SessionLocal() as session:
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
        assert {"agent.plan", "agent.step.completed", "agent.completed"} <= event_types


def test_agent_without_limits_is_rejected() -> None:
    definition = model_workflow("agent")
    definition["spec"]["nodes"][1]["config"].pop("maxSteps")

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)

    assert "AGENT_LIMITS_REQUIRED" in {item.code for item in error.value.diagnostics}


def _real_model_workflow() -> dict[str, Any]:
    definition = model_workflow("llm")
    definition["spec"]["nodes"][1]["config"].pop("modelProfile")
    return definition


def _configure_agent_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_type", "fixture")
    monkeypatch.setattr(settings, "agent_command", [sys.executable, str(STUB)])
    monkeypatch.setattr(
        settings, "agent_allowed_executables", [Path(sys.executable).name]
    )


async def test_llm_unconfigured_uses_toolless_agent_structured_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_runtime_type", "unavailable")
    _configure_agent_fixture(monkeypatch)

    run_id, run = await execute_workflow(_real_model_workflow())

    assert run.status == "succeeded"
    assert run.output_json == {"category": "fixture"}
    async with SessionLocal() as session:
        usage = await session.scalar(
            select(ModelUsage).where(ModelUsage.run_id == run_id)
        )
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
        detail = await get_run(run_id, session)
    assert usage is not None
    assert usage.runtime_used == "agent_fallback"
    assert usage.fallback_reason == "LLM_RUNTIME_UNCONFIGURED"
    assert usage.input_tokens == 12
    assert usage.output_tokens == 8
    assert usage.total_tokens == 20
    assert usage.request_dispatched_at is None
    assert usage.completed_at is not None
    assert "tool.requested" not in event_types
    assert detail["runtimeSnapshot"]["modelCalled"] is False


async def test_both_model_runtimes_unavailable_fails_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_runtime_type", "unavailable")
    monkeypatch.setattr(settings, "agent_command", None)

    _run_id, run = await execute_workflow(_real_model_workflow())

    assert run.status == "failed"
    assert run.error_json["code"] == "MODEL_RUNTIME_UNAVAILABLE"


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_code", "expected_runtime"),
    [
        (200, "succeeded", None, "direct_llm"),
        (401, "failed", "MODEL_AUTH_FAILED", None),
        (429, "failed", "RATE_LIMITED", None),
    ],
)
async def test_direct_llm_never_falls_back_after_request_started(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_status: str,
    expected_code: str | None,
    expected_runtime: str | None,
) -> None:
    settings = get_settings()
    _configure_agent_fixture(monkeypatch)
    monkeypatch.setattr(settings, "llm_runtime_type", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "direct-test-model")
    monkeypatch.setattr(settings, "llm_api_key_env", "TEST_LLM_RUNTIME_KEY")
    monkeypatch.setenv("TEST_LLM_RUNTIME_KEY", "configured")
    with ModelServer(status) as endpoint:
        monkeypatch.setattr(settings, "llm_endpoint", endpoint)
        run_id, run = await execute_workflow(_real_model_workflow())

    assert run.status == expected_status
    if expected_code:
        assert run.error_json["code"] == expected_code
    async with SessionLocal() as session:
        usage = await session.scalar(
            select(ModelUsage).where(ModelUsage.run_id == run_id)
        )
        event_types = set(
            await session.scalars(select(RunEvent.type).where(RunEvent.run_id == run_id))
        )
        detail = await get_run(run_id, session)
    assert (usage.runtime_used if usage else None) == (
        expected_runtime or "direct_llm"
    )
    assert "tool.requested" not in event_types
    assert detail["runtimeSnapshot"]["modelCalled"] is True
    if status != 200:
        assert usage is not None
        assert usage.usage_complete is False
        assert usage.total_tokens is None
        assert usage.request_dispatched_at is not None
        assert usage.completed_at is None
    else:
        assert usage is not None
        assert usage.request_dispatched_at is not None
        assert usage.completed_at is not None


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("dns failed"),
        httpx.TimeoutException("timed out"),
    ],
)
async def test_direct_llm_transport_failures_record_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    failure: httpx.HTTPError,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_runtime_type", "openai_compatible")
    monkeypatch.setattr(settings, "llm_endpoint", "https://provider.invalid/v1")
    monkeypatch.setattr(settings, "llm_model", "direct-test-model")
    monkeypatch.setattr(settings, "llm_api_key_env", "TEST_LLM_RUNTIME_KEY")
    monkeypatch.setenv("TEST_LLM_RUNTIME_KEY", "configured")

    class FailingClient:
        async def __aenter__(self) -> "FailingClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> None:
            raise failure

    monkeypatch.setattr(
        "app.runtime.models.httpx.AsyncClient",
        lambda **_kwargs: FailingClient(),
    )

    with pytest.raises(ModelRuntimeError) as raised:
        await OpenAICompatibleAdapter().generate(
            "test",
            {"type": "object"},
            10,
        )

    assert raised.value.request_dispatched is True
