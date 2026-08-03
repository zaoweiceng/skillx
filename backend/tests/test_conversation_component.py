from uuid import uuid4

import pytest

from app.api.routes import (
    ComponentConfirm,
    MessageCreate,
    confirm_component,
    create_message,
)
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
        assert response["assistant"]["type"] == "llm_response"
        assert response["assistant"]["text"] == "结构化回答"
        assert response["assistant"]["runType"] == "llm_response"
        async with SessionLocal() as session:
            run = await session.get(
                ConversationRun,
                response["assistant"]["conversationRunId"],
            )
            assert run is not None
            assert run.run_type == "llm_response"
            assert run.trajectory_json["steps"][0]["kind"] == "llm"
    finally:
        settings.model_endpoint = previous_endpoint
        settings.model_name = previous_name


async def _seed_greeting_component(name: str, description: str) -> tuple[str, str]:
    definition = deterministic_workflow()
    compiled = WorkflowCompiler().compile(definition)
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="greeting-wf",
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
            name=name,
            description=description,
            workflow_id=workflow.id,
            default_version_id=version.id,
            execution_class="zero_llm",
        )
        conversation = Conversation(id=str(uuid4()), title="候选")
        session.add_all([component, conversation])
    return component.id, conversation.id


async def test_structured_intent_matches_without_literal_name() -> None:
    # 组件名不出现在自然语言里，靠描述词元匹配。
    _, conversation_id = await _seed_greeting_component(
        "问候组件", "生成问候语 greeting 欢迎 welcome"
    )
    async with SessionLocal() as session:
        response = await create_message(
            conversation_id,
            MessageCreate(content="帮我生成一句 welcome 欢迎语"),
            session,
        )
    assistant = response["assistant"]
    assert assistant["type"] == "component_candidate"
    assert assistant["confidence"] > 0
    assert "candidateToken" in assistant
    assert assistant["inputSchema"]["required"] == ["name"]


async def test_confirm_component_starts_run_with_locked_token() -> None:
    component_id, conversation_id = await _seed_greeting_component(
        "问候组件", "生成问候语 greeting"
    )
    async with SessionLocal() as session:
        candidate = await create_message(
            conversation_id,
            MessageCreate(content="生成 greeting"),
            session,
        )
    token = candidate["assistant"]["candidateToken"]
    async with SessionLocal() as session:
        confirmed = await confirm_component(
            conversation_id,
            ComponentConfirm(
                componentId=component_id,
                candidateToken=token,
                inputs={"name": "Bob"},
            ),
            session,
        )
    assert confirmed["assistant"]["type"] == "component_run"
    run_id = confirmed["runId"]
    worker = WorkerService("confirm-worker")
    for _ in range(3):
        assert await worker.run_once()
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None and run.status == "succeeded"
        assert run.output_json == {"message": "你好，Bob"}


async def test_confirm_component_rejects_stale_token() -> None:
    component_id, conversation_id = await _seed_greeting_component(
        "问候组件", "生成问候语 greeting"
    )
    with pytest.raises(Exception) as exc_info:
        async with SessionLocal() as session:
            await confirm_component(
                conversation_id,
                ComponentConfirm(
                    componentId=component_id,
                    candidateToken="sha256:stale",
                    inputs={"name": "Bob"},
                ),
                session,
            )
    assert "409" in str(exc_info.value) or "CANDIDATE_STALE" in str(exc_info.value)
