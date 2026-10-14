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


async def test_workspace_persistence_and_history_filter(tmp_path):
    from app.api.routes import add_chat_workspace, list_chat_workspaces
    from app.api.schemas import ConversationDirectoryUpdate

    directory = str(tmp_path)
    async with SessionLocal() as session:
        for path in [directory, directory + "/."]:
            assert (
                await add_chat_workspace(
                    ConversationDirectoryUpdate(workingDirectory=path), session
                )
            )["path"] == directory
    async with SessionLocal() as session:
        assert (await list_chat_workspaces(session))["items"] == [
            {"path": directory, "name": tmp_path.name}
        ]
        session.add_all(
            [
                Conversation(id="ordinary", title="普通"),
                Conversation(
                    id="work-a",
                    title="工作一",
                    working_directory=directory,
                    workspace_path=directory,
                ),
                Conversation(
                    id="work-b",
                    title="工作二",
                    working_directory=directory,
                    workspace_path=directory,
                ),
                Conversation(id="legacy", title="旧工作区", working_directory="/legacy"),
            ]
        )
        await session.commit()
        assert (await list_chat_workspaces(session))["items"] == [
            {"path": directory, "name": tmp_path.name}
        ]
        normal = await list_conversations(workspace_path="", limit=30, query=None, session=session)
        assert [item["id"] for item in normal["items"]] == ["ordinary", "legacy"]
        first = await list_conversations(
            workspace_path=directory, limit=1, query=None, session=session
        )
        second = await list_conversations(
            workspace_path=directory,
            cursor=first["nextCursor"],
            limit=1,
            query=None,
            session=session,
        )
        assert {first["items"][0]["id"], second["items"][0]["id"]} == {"work-a", "work-b"}
        assert first["items"][0]["workingDirectory"] == directory
        assert second["nextCursor"] is None


async def test_workspace_edit_and_delete_preserves_execution_directory(tmp_path):
    from app.api.routes import (
        add_chat_workspace,
        create_conversation,
        delete_chat_workspace,
        list_chat_workspaces,
        update_chat_workspace,
        update_conversation_directory,
    )
    from app.api.schemas import ChatWorkspaceUpdate, ConversationCreate, ConversationDirectoryUpdate

    old = str(tmp_path)
    new = tmp_path / "next"
    new.mkdir()
    async with SessionLocal() as session:
        await add_chat_workspace(ConversationDirectoryUpdate(workingDirectory=old), session)
        workspace_chat = await create_conversation(ConversationCreate(workspacePath=old), session)
        ordinary = await create_conversation(ConversationCreate(), session)
        await update_conversation_directory(
            ordinary["id"], ConversationDirectoryUpdate(workingDirectory=old), session
        )
        normal = await list_conversations(workspace_path="", limit=30, query=None, session=session)
        assert [item["id"] for item in normal["items"]] == [ordinary["id"]]
        await update_chat_workspace(ChatWorkspaceUpdate(path=old, name="  我的项目  "), session)
        assert (await list_chat_workspaces(session))["items"] == [{"path": old, "name": "我的项目"}]
        await update_chat_workspace(
            ChatWorkspaceUpdate(path=old, workingDirectory=str(new)), session
        )
        member = await session.get(Conversation, workspace_chat["id"])
        assert member.workspace_path == member.working_directory == str(new)
        assert (await session.get(Conversation, ordinary["id"])).working_directory == old
        await delete_chat_workspace(str(new), False, session)
        assert member.workspace_path is None
        assert member.working_directory == str(new)
        assert member.deleted_at is None
        assert (await list_chat_workspaces(session))["items"] == []
        await add_chat_workspace(ConversationDirectoryUpdate(workingDirectory=str(new)), session)
        grouped = await list_conversations(
            workspace_path=str(new), limit=30, query=None, session=session
        )
        assert grouped["items"] == []


async def test_delete_workspace_cancels_only_member_tasks(tmp_path):
    from app.api.routes import add_chat_workspace, create_conversation, delete_chat_workspace
    from app.api.schemas import ConversationCreate, ConversationDirectoryUpdate

    path = str(tmp_path)
    async with SessionLocal() as session:
        await add_chat_workspace(ConversationDirectoryUpdate(workingDirectory=path), session)
        member = await create_conversation(ConversationCreate(workspacePath=path), session)
        ordinary = await create_conversation(ConversationCreate(), session)
        result = await create_message(member["id"], MessageCreate(content="任务"), session)
        await delete_chat_workspace(path, True, session)
        assert (await session.get(Task, result["taskId"])).status == "cancelled"
        assert (await session.get(Conversation, member["id"])).deleted_at is not None
        assert (await session.get(Conversation, ordinary["id"])).deleted_at is None
        assert tmp_path.is_dir()
        with pytest.raises(HTTPException) as exc:
            await create_conversation(ConversationCreate(workspacePath=path), session)
        assert exc.value.status_code == 404


async def test_workspace_directory_collision_is_rejected(tmp_path):
    from app.api.routes import add_chat_workspace, update_chat_workspace
    from app.api.schemas import ChatWorkspaceUpdate, ConversationDirectoryUpdate

    other = tmp_path / "other"
    other.mkdir()
    async with SessionLocal() as session:
        for path in [str(tmp_path), str(other)]:
            await add_chat_workspace(ConversationDirectoryUpdate(workingDirectory=path), session)
        with pytest.raises(HTTPException) as exc:
            await update_chat_workspace(
                ChatWorkspaceUpdate(path=str(tmp_path), workingDirectory=str(other)), session
            )
        assert exc.value.status_code == 409
