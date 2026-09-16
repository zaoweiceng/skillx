import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.runtime_profiles import Selection, select_agent, traex_models
from app.runtime.traex_models import list_traex_models, validate_traex_model


async def test_cli_model_list_uses_config_id_and_deduplicates(tmp_path, monkeypatch):
    executable = tmp_path / "traex"
    executable.write_text(
        f'#!{sys.executable}\nimport json, sys\n'
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
