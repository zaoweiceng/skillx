from uuid import uuid4

import pytest

from app.api.routes import (
    ComponentConfirm,
    MessageCreate,
    confirm_component,
    create_message,
)
from app.application.agent_host import LOCAL_DEMO_READ_ONLY_CAPABILITIES
from app.application.compiler import WorkflowCompiler
from app.application.conversation import candidate_snapshot_digest
from app.application.orchestrator import WorkerService
from app.infrastructure.database import (
    Component,
    Conversation,
    ConversationRun,
    Run,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)
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


async def test_unbound_conversation_continues_with_persistent_agent_run() -> None:
    async with SessionLocal() as session, session.begin():
        conversation = Conversation(id=str(uuid4()), title="通用 Agent 对话")
        session.add(conversation)
    async with SessionLocal() as session:
        response = await create_message(
            conversation.id,
            MessageCreate(content="分析这个新任务"),
            session,
        )
    assert response["assistant"]["type"] == "agent_run"
    assert response["assistant"]["state"] == "queued"
    assert response["assistant"]["runType"] == "agent_run"
    assert response["assistant"]["agentAction"] == "continue_agent"
    async with SessionLocal() as session:
        run = await session.get(
            ConversationRun,
            response["assistant"]["conversationRunId"],
        )
    assert run is not None
    assert run.run_type == "agent_run"
    assert run.status == "queued"
    assert tuple(run.config_json["allowedCapabilities"]) == (
        LOCAL_DEMO_READ_ONLY_CAPABILITIES
    )


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


async def test_continue_agent_does_not_use_undisclosed_component_heuristic() -> None:
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
    assert assistant["type"] == "agent_run"
    assert assistant["agentAction"] == "continue_agent"
    assert "candidateSnapshotDigest" not in assistant


async def test_confirm_component_starts_run_with_locked_token() -> None:
    component_id, conversation_id = await _seed_greeting_component(
        "问候组件", "生成问候语 greeting"
    )
    async with SessionLocal() as session:
        component = await session.get(Component, component_id)
        version = await session.get(WorkflowVersion, component.default_version_id)
        token = candidate_snapshot_digest(
            component.id,
            version.id,
            version.definition_json["spec"]["inputSchema"],
        )
    async with SessionLocal() as session:
        confirmed = await confirm_component(
            conversation_id,
            ComponentConfirm(
                componentId=component_id,
                candidateSnapshotDigest=token,
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
                    candidateSnapshotDigest="sha256:stale",
                    inputs={"name": "Bob"},
                ),
                session,
            )
    assert "409" in str(exc_info.value) or "CANDIDATE_STALE" in str(exc_info.value)
