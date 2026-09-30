import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.runtime_profiles import Selection, select_agent, traex_models
from app.runtime.traex_models import list_traex_models, validate_traex_model


async def test_cli_model_list_uses_config_id_and_deduplicates(tmp_path, monkeypatch):
    executable = tmp_path / "traex"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        'assert sys.argv[1:] == ["models", "--json"]\n'
        'print(json.dumps([{"name":"Display", "config_name":"model-id"},'
        '{"name":"fallback"},{"name":"Display", "config_name":"model-id"}]))\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _: str(executable))
    assert [item["id"] for item in await list_traex_models()] == ["model-id", "fallback"]
    await validate_traex_model("model-id")
    with pytest.raises(ValueError, match="不在当前"):
        await validate_traex_model("unknown")


async def test_model_list_failure_is_actionable(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(HTTPException) as error:
        await traex_models()
    assert error.value.status_code == 503
    assert "未安装" in error.value.detail


async def test_selection_rejects_unknown_model_without_changing_conversation(monkeypatch):
    from app.infrastructure.database import Conversation

    conversation = Conversation(id="test", traex_model="original")
    session = AsyncMock()
    session.get.return_value = conversation
    monkeypatch.setattr(
        "app.api.runtime_profiles.validate_traex_model",
        AsyncMock(side_effect=ValueError("无效模型")),
    )
    with pytest.raises(HTTPException) as error:
        await select_agent("test", Selection(traexModel="bad"), session)
    assert error.value.status_code == 422
    assert conversation.traex_model == "original"
    session.commit.assert_not_called()


async def test_reasoning_selection_persists_and_is_frozen_per_task(monkeypatch):
    from dataclasses import replace
    from uuid import uuid4

    from app.api.routes import create_message
    from app.api.runtime_profiles import get_selection
    from app.api.schemas import MessageCreate
    from app.application.runtime_config import agent_runtime_config
    from app.infrastructure.database import Conversation, SessionLocal
    from app.infrastructure.models.tasks import Task

    runtime = replace(agent_runtime_config(), type="traex_builtin", model=None)
    monkeypatch.setattr("app.application.runtime_profiles.agent_runtime_config", lambda: runtime)
    models = [{"id": "gpt-test", "reasoningEfforts": ["low", "medium", "high"]}]
    monkeypatch.setattr(
        "app.runtime.traex_models.list_traex_models", AsyncMock(return_value=models)
    )
    conversation_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id=conversation_id))
    async with SessionLocal() as session:
        await select_agent(
            conversation_id, Selection(traexModel="gpt-test", reasoningEffort="high"), session
        )
        result = await create_message(conversation_id, MessageCreate(content="测试推理"), session)
        await select_agent(
            conversation_id, Selection(traexModel="gpt-test", reasoningEffort="low"), session
        )
    async with SessionLocal() as session:
        assert (await get_selection(conversation_id, session))["reasoningEffort"] == "low"
        task = await session.get(Task, result["taskId"])
        assert task.runtime_json["reasoning_effort"] == "high"
        with pytest.raises(HTTPException) as error:
            await select_agent(conversation_id, Selection(reasoningEffort="high"), session)
        assert error.value.status_code == 422
        assert (await get_selection(conversation_id, session))["reasoningEffort"] == "low"
