from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.routes import (
    create_message,
    delete_conversation,
    get_conversation,
    list_conversations,
    rename_conversation,
)
from app.api.schemas import ConversationTitleUpdate, MessageCreate
from app.infrastructure.database import Conversation, SessionLocal
from app.infrastructure.models.tasks import Task


async def test_history_rename_delete_persistence():
    target, other = str(uuid4()), str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add_all(
            [Conversation(id=target, title="旧标题"), Conversation(id=other, title="保留")]
        )
    async with SessionLocal() as session:
        await rename_conversation(target, ConversationTitleUpdate(title="  新标题  "), session)
        result = await create_message(target, MessageCreate(content="任务"), session)
    async with SessionLocal() as session:
        assert (await get_conversation(target, session))["title"] == "新标题"
        await delete_conversation(target, session)
        await delete_conversation(target, session)
    async with SessionLocal() as session:
        page = await list_conversations(cursor=None, limit=30, query=None, session=session)
        assert [x["id"] for x in page["items"]] == [other]
        assert (await session.get(Task, result["taskId"])).status == "cancelled"
        for action in [
            get_conversation(target, session),
            rename_conversation(target, ConversationTitleUpdate(title="恢复"), session),
            create_message(target, MessageCreate(content="再次发送"), session),
        ]:
            with pytest.raises(HTTPException) as exc:
                await action
            assert exc.value.status_code == 404


def test_title_validation():
    for title in ["", "   ", "字" * 201]:
        with pytest.raises(ValidationError):
            ConversationTitleUpdate(title=title)
