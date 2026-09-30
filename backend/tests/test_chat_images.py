import base64
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.routes import chat_image, create_message
from app.api.schemas import MessageCreate
from app.application.agent_host import AgentHost, AgentHostResult
from app.application.chat_images import image_path
from app.application.runtime_config import agent_runtime_config
from app.application.tasks import TaskDriver
from app.config import get_settings
from app.infrastructure.database import Conversation, Message, SessionLocal
from app.infrastructure.models.tasks import Task

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aBZkAAAAASUVORK5CYII="
IMAGE = {"name": "截图.png", "mimeType": "image/png", "data": PNG}


async def test_images_survive_reload_reach_agent_and_follow_up(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path)
    runtime = replace(agent_runtime_config(), type="pi", model_options={"images": True})

    async def snapshot(*args, **kwargs):
        return asdict(runtime)

    monkeypatch.setattr("app.application.runtime_profiles.agent_snapshot", snapshot)
    monkeypatch.setattr("app.application.tasks.agent_snapshot", snapshot)
    conversation_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id=conversation_id))
    async with SessionLocal() as session:
        result = await create_message(
            conversation_id, MessageCreate(content="", images=[IMAGE]), session
        )
    async with SessionLocal() as session:
        message = await session.get(Message, result["userMessageId"])
        task = await session.get(Task, result["taskId"])
        reference = message.content_json["images"][0]
        assert task.state_json["images"] == [reference]
        assert "data" not in reference
        assert image_path(reference).read_bytes() == base64.b64decode(PNG)
        assert image_path(reference).stat().st_mode & 0o777 == 0o600
        served = await chat_image(message.id, reference["id"], session)
        assert served.media_type == "image/png"
        with pytest.raises(HTTPException):
            await chat_image(message.id, str(uuid4()), session)
    requests = []

    async def execute(self, request, **kwargs):
        requests.append(request)
        return AgentHostResult(
            output={
                "action": "complete",
                "summary": "已查看",
                "answer": "图片已查看",
                "completionCriteria": ["查看图片"],
            }
        )

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    assert requests[0].images == [{"path": str(image_path(reference)), "mimeType": "image/png"}]
    assert PNG not in requests[0].prompt
    async with SessionLocal() as session:
        await create_message(conversation_id, MessageCreate(content="继续分析刚才的图"), session)
    assert await TaskDriver().poll_once()
    assert requests[1].images == requests[0].images


async def test_text_only_model_rejects_images_before_acceptance(monkeypatch):
    async def snapshot(*args, **kwargs):
        return {"type": "pi", "model_options": {"images": False}}

    monkeypatch.setattr("app.application.runtime_profiles.agent_snapshot", snapshot)
    conversation_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id=conversation_id))
    async with SessionLocal() as session:
        with pytest.raises(HTTPException, match="支持图片"):
            await create_message(
                conversation_id, MessageCreate(content="test", images=[IMAGE]), session
            )
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


@pytest.mark.parametrize(
    "image",
    [
        {**IMAGE, "data": "not base64!"},
        {**IMAGE, "data": base64.b64encode(b"not an image").decode()},
        {**IMAGE, "mimeType": "image/svg+xml"},
        {**IMAGE, "mimeType": "image/jpeg"},
        {
            **IMAGE,
            "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)).decode(),
        },
    ],
)
def test_invalid_images_are_rejected(image):
    with pytest.raises(ValidationError):
        MessageCreate(content="test", images=[image])


def test_image_count_and_total_size_limits():
    with pytest.raises(ValidationError):
        MessageCreate(content="test", images=[IMAGE] * 9)
    large = {
        **IMAGE,
        "data": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (4 * 1024 * 1024)).decode(),
    }
    with pytest.raises(ValidationError):
        MessageCreate(content="test", images=[large] * 5)
