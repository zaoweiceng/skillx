"""API credential isolation, model selection and the default migration contract."""

import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.application.runtime_profiles import agent_snapshot, initialize_profiles, save_profile
from app.config import get_settings
from app.infrastructure.database import Conversation, RuntimeProfile, SessionLocal
from app.infrastructure.runtime_credentials import resolve_runtime_key
from app.main import app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def test_model_service_keys_are_private_and_rotations_preserve_snapshots(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "runtime_credentials_path", tmp_path / "keys")
    body = {
        "name": "service",
        "kind": "model",
        "config": {
            "baseUrl": "http://localhost:9999/v1",
            "apiKey": "private-original",
            "models": [{"id": "first"}, {"id": "second", "reasoning": True}],
        },
    }
    response = await client.post("/api/runtime-profiles", json=body)
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["isDefault"] and profile["apiKeyConfigured"]
    assert "private-original" not in response.text
    reference = profile["config"]["apiKeyEnv"]
    assert resolve_runtime_key(reference) == "private-original"
    assert (tmp_path / "keys" / reference).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "keys").stat().st_mode & 0o777 == 0o700
    async with SessionLocal() as session, session.begin():
        pi = await save_profile(
            session,
            profile_id=None,
            name="Pi",
            kind="agent",
            config={"adapter": "pi"},
            is_default=True,
            active=True,
        )
        conversation = Conversation(id=str(uuid4()), title="choice")
        session.add(conversation)
        await session.flush()
        snapshot = await agent_snapshot(session, pi.id, None, "high", profile["id"], "second")
        assert snapshot["model"] == "second" and snapshot["model_options"]["reasoning"]
        assert "private-original" not in json.dumps(snapshot)
        conversation_id = conversation.id
    selection = {"modelProfileId": profile["id"], "model": "second", "reasoningEffort": "high"}
    selected = await client.put(
        f"/api/runtime-profiles/conversations/{conversation_id}", json=selection
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["model"] == "second"
    # Blank edits preserve the key, while rotations keep the previous immutable reference valid.
    body["config"].pop("apiKey")
    edited = await client.put(f"/api/runtime-profiles/{profile['id']}", json=body)
    assert edited.json()["config"]["apiKeyEnv"] == reference
    body["config"]["apiKey"] = "private-rotated"
    edited = await client.put(f"/api/runtime-profiles/{profile['id']}", json=body)
    assert edited.status_code == 200
    assert resolve_runtime_key(edited.json()["config"]["apiKeyEnv"]) == "private-rotated"
    assert resolve_runtime_key(snapshot["api_key_env"]) == "private-original"
    assert "private" not in (await client.get("/api/runtime-profiles")).text
    invalid = await client.put(
        f"/api/runtime-profiles/conversations/{conversation_id}",
        json={"modelProfileId": profile["id"], "model": "missing"},
    )
    assert invalid.status_code == 422
    async with SessionLocal() as session:
        saved = await session.get(RuntimeProfile, profile["id"])
        assert "private" not in json.dumps(saved.config_json)


async def test_invalid_configuration_does_not_echo_credentials(client):
    response = await client.post(
        "/api/runtime-profiles",
        json={
            "name": "bad",
            "kind": "model",
            "config": {
                "baseUrl": "invalid",
                "apiKey": "never-echo-this",
                "models": [{"id": "test"}],
            },
        },
    )
    assert response.status_code == 422
    assert "never-echo-this" not in response.text


async def test_builtin_pi_migrates_existing_default_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_builtin_enabled", True)
    async with SessionLocal() as session:
        session.add(
            RuntimeProfile(
                id="builtin-traex",
                name="TraeX",
                kind="agent",
                config_json={"adapter": "traex"},
                active=True,
                is_default=True,
            )
        )
        session.add(
            Conversation(
                id="old-choice",
                title="old",
                agent_profile_id="builtin-traex",
                traex_model="old-model",
            )
        )
        await session.commit()
        await initialize_profiles(session)
        default = await session.scalar(
            select(RuntimeProfile).where(RuntimeProfile.is_default.is_(True))
        )
        assert default.id == "builtin-pi"
        conversation = await session.get(Conversation, "old-choice", populate_existing=True)
        assert conversation.agent_profile_id is None and conversation.traex_model is None
        assert (await agent_snapshot(session, None))["type"] == "pi"
        await initialize_profiles(session)
        assert len((await session.scalars(select(RuntimeProfile))).all()) == 2


async def test_saved_pi_service_executes_task_and_persists_complete_usage(
    client, monkeypatch, tmp_path
):
    import asyncio

    from app.application.product_history import usage_summary
    from app.application.tasks import TaskDriver
    from app.infrastructure.database import ConversationTurn
    from app.infrastructure.models.tasks import TaskEvent

    monkeypatch.setattr(get_settings(), "agent_builtin_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_credentials_path", tmp_path / "keys")
    monkeypatch.setattr(get_settings(), "artifact_root", tmp_path / "artifacts")
    received = []
    answer = {
        "action": "complete",
        "summary": "完成",
        "completionCriteria": ["回答"],
        "answer": "收到",
    }

    async def respond(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        length = int(
            next(
                line.split(b":", 1)[1]
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        received.append(json.loads(await reader.readexactly(length)))
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": json.dumps(answer)},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 10,
                    "total_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
            },
        ]
        body = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        async with SessionLocal() as session:
            await initialize_profiles(session)
        response = await client.post(
            "/api/runtime-profiles",
            json={
                "name": "local",
                "kind": "model",
                "config": {
                    "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
                    "apiKey": "local-private-key",
                    "models": [{"id": "first"}, {"id": "selected", "maxOutputTokens": 1024}],
                },
            },
        )
        assert response.status_code == 201
        profile_id = response.json()["id"]
        conversation = (
            await client.post("/api/conversations", json={"title": "Pi full chain"})
        ).json()
        selected = await client.put(
            f"/api/runtime-profiles/conversations/{conversation['id']}",
            json={"modelProfileId": profile_id, "model": "selected"},
        )
        assert selected.status_code == 200
        accepted = await client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"content": "直接回答收到，禁止其他操作。" + "中文上下文" * 2500},
        )
        assert accepted.status_code == 202, accepted.text
        task_id = accepted.json()["taskId"]
        assert await TaskDriver().poll_once()
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        assert task["status"] == "succeeded", task
        assert received[0]["model"] == "selected"
        assert received[0].get("max_tokens", received[0].get("max_completion_tokens")) == 1024
        async with SessionLocal() as session:
            turn = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.conversation_id == conversation["id"]
                )
            )
            usage = await usage_summary(session, turn.id)
            assert usage["state"] == "complete" and usage["totalTokens"] == 40
            assert usage["stages"][0]["cacheReadTokens"] == 5
            rows = (
                await session.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id))
            ).all()
            traces = [
                row.payload_json["payload"]
                for row in rows
                if row.type == "agent.audit" and row.payload_json["type"] == "model.trace"
            ]
            request_chunks = sorted(
                (trace for trace in traces if trace["phase"] == "request"),
                key=lambda trace: trace["chunkIndex"],
            )
            assert len(request_chunks) > 1
            assert json.loads("".join(trace["data"] for trace in request_chunks)) == received[0]
        persisted = (await client.get(f"/api/tasks/{task_id}/events")).text
        assert "model.trace" in persisted and "local-private-key" not in persisted
    finally:
        server.close()
        await server.wait_closed()
