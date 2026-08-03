from uuid import uuid4

import pytest

from app.api.routes import MessageCreate, create_message
from app.application.compiler import WorkflowCompiler
from app.application.orchestrator import WorkerService
from app.config import get_settings
from app.infrastructure.database import (
    Component,
    Conversation,
    ConversationRun,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)
from app.runtime.models import ModelResult, OpenAICompatibleAdapter
from tests.fixtures import deterministic_workflow


async def test_conversation_confirmed_component_hands_control_to_workflow() -> None:
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="conversation-greeting",
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
        component = Component(
            id=str(uuid4()),
            name="问候组件",
            workflow_id=workflow.id,
            default_version_id=version.id,
            execution_class="zero_llm",
        )
        conversation = Conversation(id=str(uuid4()), title="组件调用")
        session.add_all([component, conversation])
    async with SessionLocal() as session:
        response = await create_message(
            conversation.id,
            MessageCreate(
                content="请执行问候组件",
                componentId=component.id,
                inputs={"name": "Alice"},
            ),
            session,
        )

    assert response["assistant"]["type"] == "component_run"
    run_id = response["assistant"]["runId"]
    worker = WorkerService("conversation-worker")
    for _ in range(3):
        assert await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.status == "succeeded"
        assert run.output_json == {"message": "你好，Alice"}


async def test_unbound_conversation_uses_configured_model_and_records_trajectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    previous_endpoint = settings.model_endpoint
    previous_name = settings.model_name
    settings.model_endpoint = "http://model.test/v1/chat/completions"
    settings.model_name = "test-model"

    async def fake_generate(
        _self: OpenAICompatibleAdapter,
        _prompt: str,
        _schema: dict,
        _max_tokens: int,
    ) -> ModelResult:
        return ModelResult(
            output={"reply": "结构化回答"},
            input_tokens=10,
            output_tokens=4,
            model="test-model",
        )

    monkeypatch.setattr(OpenAICompatibleAdapter, "generate", fake_generate)
    try:
        async with SessionLocal() as session, session.begin():
            conversation = Conversation(id=str(uuid4()), title="模型对话")
            session.add(conversation)
        async with SessionLocal() as session:
            response = await create_message(
                conversation.id,
                MessageCreate(content="分析这个新任务"),
                session,
            )
        assert response["assistant"]["type"] == "agent_run"
        assert response["assistant"]["text"] == "结构化回答"
        async with SessionLocal() as session:
            run = await session.get(
                ConversationRun,
                response["assistant"]["conversationRunId"],
            )
            assert run is not None
            assert run.trajectory_json["steps"][0]["kind"] == "llm"
    finally:
        settings.model_endpoint = previous_endpoint
        settings.model_name = previous_name
