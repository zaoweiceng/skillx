from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.routes import create_message
from app.api.schemas import MessageCreate
from app.application.agent_host import AgentHost, AgentHostResult
from app.application.tasks import TaskDriver
from app.infrastructure.database import Conversation, Message, SessionLocal
from app.infrastructure.models.tasks import Task


async def test_reference_files_are_durable_and_reach_agent(monkeypatch):
    conversation_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id=conversation_id, title="参考文件测试"))
    references = [{"name": "requirements.md", "content": "交付目标：支持导出 CSV"}]
    async with SessionLocal() as session:
        response = await create_message(
            conversation_id,
            MessageCreate(content="总结要求", referenceFiles=references),
            session,
        )
    async with SessionLocal() as session:
        task = await session.get(Task, response["taskId"])
        assert task.state_json["referenceFiles"] == references
        message = await session.get(Message, response["userMessageId"])
        assert message.content_json["referenceFiles"] == [{"name": "requirements.md"}]
        assert message.content_json["text"] == "总结要求"

    prompts = []

    async def execute(self, request, **kwargs):
        prompts.append(request.prompt)
        return AgentHostResult(output={
            "action": "complete", "summary": "已总结", "answer": "支持导出 CSV",
            "completionCriteria": ["总结要求"],
        })

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    assert "交付目标：支持导出 CSV" in prompts[0]


def test_reference_limits_are_enforced_server_side():
    with pytest.raises(ValidationError):
        MessageCreate(
            content="test", referenceFiles=[{"name": "large.txt", "content": "文" * 50000}]
        )
    with pytest.raises(ValidationError):
        MessageCreate(content="test", referenceFiles=[{"name": "binary", "content": "a\0b"}])
    with pytest.raises(ValidationError):
        MessageCreate(content="test", referenceFiles=[{"name": "x", "content": ""}] * 9)
