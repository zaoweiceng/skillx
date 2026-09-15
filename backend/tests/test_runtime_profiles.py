"""Profile selections are durable, typed and isolated from later edits."""

from dataclasses import asdict
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.runtime_profiles import ProfileInput
from app.application.agent_run import enqueue_agent_run
from app.application.runtime_profiles import agent_snapshot, restore_agent_snapshot, save_profile
from app.infrastructure.database import Conversation, SessionLocal


def test_credentials_must_be_references() -> None:
    with pytest.raises(ValidationError):
        ProfileInput.model_validate(
            {
                "name": "bad",
                "kind": "model",
                "config": {
                    "baseUrl": "https://example.com/v1",
                    "model": "test",
                    "apiKey": "secret",
                },
            }
        )


async def test_queued_run_keeps_pi_config_after_profile_edit(monkeypatch):
    monkeypatch.setenv("PROFILE_TEST_KEY", "local-test-value")
    async with SessionLocal() as session, session.begin():
        model = await save_profile(
            session,
            profile_id=None,
            name="model",
            kind="model",
            config={
                "baseUrl": "http://localhost:9123/v1",
                "model": "original",
                "apiKeyEnv": "PROFILE_TEST_KEY",
            },
            is_default=True,
            active=True,
        )
        agent = await save_profile(
            session,
            profile_id=None,
            name="pi",
            kind="agent",
            config={"adapter": "pi", "modelProfileId": model.id},
            is_default=False,
            active=True,
        )
        conversation = Conversation(id=str(uuid4()), title="isolated", agent_profile_id=agent.id)
        session.add(conversation)
        await session.flush()
        run = await enqueue_agent_run(session, conversation, "hello")
        snapshot = run.config_json["agentRuntimeSnapshot"]
        assert snapshot["type"] == "pi"
        assert snapshot["model"] == "original"
        assert "local-test-value" not in str(snapshot)
        await save_profile(
            session,
            profile_id=model.id,
            name="model",
            kind="model",
            config={**model.config_json, "model": "changed"},
            is_default=True,
            active=True,
        )
        assert (await agent_snapshot(session, agent.id))["model"] == "changed"
        assert run.config_json["agentRuntimeSnapshot"]["model"] == "original"
        assert asdict(restore_agent_snapshot(snapshot)) == {
            **snapshot,
            "command": tuple(snapshot["command"]),
            "allowed_executables": tuple(snapshot["allowed_executables"]),
        }


async def test_inactive_agent_is_not_silently_replaced():
    async with SessionLocal() as session, session.begin():
        agent = await save_profile(
            session,
            profile_id=None,
            name="disabled",
            kind="agent",
            config={"adapter": "traex"},
            is_default=False,
            active=False,
        )
        with pytest.raises(ValueError, match="不可用"):
            await agent_snapshot(session, agent.id)


async def test_pi_default_requires_verified_current_configuration():
    async with SessionLocal() as session, session.begin():
        with pytest.raises(ValueError, match="探测成功"):
            await save_profile(
                session,
                profile_id=None,
                name="pi",
                kind="agent",
                config={"adapter": "pi", "modelProfileId": "missing"},
                is_default=True,
                active=True,
            )
