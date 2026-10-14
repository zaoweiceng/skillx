"""Task integration: persisted model inputs and evidence survive follow-up goals."""

import json
from dataclasses import replace
from datetime import timedelta

from app.application.agent_host import AgentHost, AgentHostResult
from app.application.conversation_context import load_context
from app.application.runtime_config import agent_runtime_config
from app.application.tasks import TaskDriver
from app.infrastructure.database import SessionLocal
from app.infrastructure.models import Conversation, Invocation, Task, utcnow


def complete(evidence=()):
    return {
        "action": "complete",
        "summary": "已有证据足够",
        "completionCriteria": ["回答问题"],
        "answer": "42",
        "evidence": list(evidence),
    }


async def test_followup_reuses_actual_model_messages_and_prior_task_evidence(monkeypatch):
    monkeypatch.setattr(
        "app.application.tasks.restore_agent_snapshot",
        lambda snapshot: replace(agent_runtime_config(), type="pi"),
    )
    async with SessionLocal() as session, session.begin():
        session.add_all([Conversation(id="context-a"), Conversation(id="context-b")])
        await session.flush()
        session.add(Task(id="first", conversation_id="context-a", goal="读取结果"))
        await session.flush()
        session.add(
            Invocation(
                id="successful-read",
                task_id="first",
                scope="first",
                call_key="read",
                kind="tool",
                status="succeeded",
                request_json={},
                output_json={"result": 42},
            )
        )
    actual_input = {"role": "user", "content": "实际发送给模型的上下文：42"}
    first_output = complete(["successful-read"])
    requests = []

    async def execute(self, request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            return AgentHostResult(output=first_output, input_message=actual_input)
        if len(requests) == 2:
            assert request.messages[0]["content"] == actual_input["content"]
            assert request.messages[0]["role"] == "user"
            assert request.messages[1]["role"] == "assistant"
            assert json.loads(request.messages[1]["content"])["answer"] == "42"
            assert (
                "successful-read"
                in request.output_schema["properties"]["evidence"]["items"]["enum"]
            )
            return AgentHostResult(
                output=complete(["successful-read"]),
                input_message={"role": "user", "content": "追问上下文"},
            )
        assert request.messages == []
        assert "successful-read" not in request.prompt
        return AgentHostResult(output=complete(), input_message={"role": "user", "content": "隔离"})

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        first = await session.get(Task, "first")
        assert first.status == "succeeded", first.error_json
        session.add(Task(id="second", conversation_id="context-a", goal="为什么是这个结果"))
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        assert (await session.get(Task, "second")).status == "succeeded"
        assert len((await load_context(session, "context-a")).exchanges) == 2
        session.add(Task(id="isolated", conversation_id="context-b", goal="独立问题"))
    assert await TaskDriver().poll_once()
    assert len(requests) == 3


async def test_unfinished_earlier_goal_blocks_only_its_own_conversation(monkeypatch):
    monkeypatch.setattr(
        "app.application.tasks.restore_agent_snapshot",
        lambda snapshot: replace(agent_runtime_config(), type="pi"),
    )
    async with SessionLocal() as session, session.begin():
        session.add_all([Conversation(id="serial-a"), Conversation(id="serial-b")])
        await session.flush()
        session.add_all(
            [
                Task(
                    id="waiting",
                    conversation_id="serial-a",
                    goal="先完成",
                    status="waiting_execution",
                    created_at=utcnow() - timedelta(seconds=10),
                ),
                Task(id="later", conversation_id="serial-a", goal="后处理"),
                Task(id="independent", conversation_id="serial-b", goal="独立处理"),
            ]
        )
        await session.flush()
        session.add(
            Invocation(
                id="pending",
                task_id="waiting",
                scope="waiting",
                call_key="pending",
                kind="tool",
                status="running",
                request_json={},
            )
        )
    seen = []

    async def execute(self, request, **kwargs):
        seen.append(request.prompt)
        return AgentHostResult(output=complete(), input_message={"role": "user", "content": "处理"})

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        assert (await session.get(Task, "independent")).status == "succeeded"
        assert (await session.get(Task, "later")).status == "queued"
        (await session.get(Task, "waiting")).status = "cancelled"
        (await session.get(Invocation, "pending")).status = "cancelled"
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, "later")).status == "succeeded"
    assert len(seen) == 2


async def test_checkpoint_starts_new_stable_epoch_and_retains_originals(monkeypatch):
    from app.application.conversation_context import append_exchange
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "conversation_context_max_characters", 8192)
    monkeypatch.setattr(
        "app.application.tasks.restore_agent_snapshot",
        lambda snapshot: replace(agent_runtime_config(), type="pi"),
    )
    original = {"role": "user", "content": "original" * 2000}
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id="epoch"))
        await session.flush()
        session.add(Task(id="epoch-old", conversation_id="epoch", goal="old", status="succeeded"))
        await session.flush()
        await append_exchange(session, "epoch", "epoch-old", "old", original, "old answer", [])
        session.add(Task(id="epoch-new", conversation_id="epoch", goal="new"))
    captured = []

    async def execute(self, request, **kwargs):
        captured.append(request)
        if len(captured) == 1:
            assert request.messages == []
            assert "contextCheckpoint" in request.prompt
        return AgentHostResult(
            output=complete(), input_message={"role": "user", "content": request.prompt}
        )

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session, session.begin():
        active = await load_context(session, "epoch")
        assert len(active.exchanges) == 1
        assert active.exchanges[0].is_checkpoint
        prefix = active.messages
        all_history = await load_context(session, "epoch", after_id=0)
        assert len(all_history.exchanges) == 2
        assert all_history.messages[0] == original
        session.add(Task(id="epoch-next", conversation_id="epoch", goal="followup"))
    assert await TaskDriver().poll_once()
    assert captured[1].messages == prefix
    async with SessionLocal() as session:
        assert (await load_context(session, "epoch")).messages[:2] == prefix


async def test_rejected_or_stale_decisions_never_append_context(monkeypatch):
    monkeypatch.setattr(
        "app.application.tasks.restore_agent_snapshot",
        lambda snapshot: replace(agent_runtime_config(), type="pi"),
    )
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id="attempts"))
        await session.flush()
        session.add(Task(id="attempt", conversation_id="attempts", goal="answer"))

    async def rejected(self, request, **kwargs):
        return AgentHostResult(
            output=complete(["never-observed"]),
            input_message={"role": "user", "content": "rejected"},
        )

    monkeypatch.setattr(AgentHost, "execute", rejected)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await session.get(Task, "attempt")).status == "queued"
        assert (await load_context(session, "attempts", after_id=0)).exchanges == []

    async def stale(self, request, **kwargs):
        async with SessionLocal() as session, session.begin():
            current = await session.get(Task, "attempt")
            current.lease_token = "replacement-lease"
        return AgentHostResult(
            output=complete(), input_message={"role": "user", "content": "stale"}
        )

    monkeypatch.setattr(AgentHost, "execute", stale)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        assert (await load_context(session, "attempts", after_id=0)).exchanges == []


async def test_unrelated_followup_can_complete_without_citing_old_evidence(monkeypatch):
    from app.application.conversation_context import append_exchange

    monkeypatch.setattr(
        "app.application.tasks.restore_agent_snapshot",
        lambda snapshot: replace(agent_runtime_config(), type="pi"),
    )
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id="greeting"))
        await session.flush()
        session.add(
            Task(id="read-old", conversation_id="greeting", goal="read", status="succeeded")
        )
        await session.flush()
        await append_exchange(
            session,
            "greeting",
            "read-old",
            "read",
            {"role": "user", "content": "read result"},
            json.dumps(complete(["old-evidence"])),
            [
                {
                    "id": "old-evidence",
                    "key": "old",
                    "taskId": "read-old",
                    "kind": "tool",
                    "status": "succeeded",
                    "request": {},
                    "output": 42,
                    "error": None,
                }
            ],
        )
        session.add(Task(id="hello", conversation_id="greeting", goal="你好"))

    async def execute(self, request, **kwargs):
        assert "old-evidence" in request.output_schema["properties"]["evidence"]["items"]["enum"]
        return AgentHostResult(
            output={**complete(), "answer": "你好！"},
            input_message={"role": "user", "content": request.prompt},
        )

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, "hello")
        assert task.status == "succeeded", task.error_json
        assert len((await load_context(session, "greeting")).exchanges) == 2


async def test_image_history_over_limit_checkpoints_without_resending_old_images(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from app.application.conversation_context import append_exchange
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "task_local_execution_enabled", True)
    runtime = replace(agent_runtime_config(), type="pi", model_options={"images": True})
    monkeypatch.setattr("app.application.tasks.restore_agent_snapshot", lambda snapshot: runtime)
    images = []
    for index in range(9):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        images.append({"path": str(path), "mimeType": "image/png"})
    async with SessionLocal() as session, session.begin():
        session.add(Conversation(id="images"))
        await session.flush()
        session.add(
            Task(id="images-old", conversation_id="images", goal="inspect", status="succeeded")
        )
        await session.flush()
        await append_exchange(
            session,
            "images",
            "images-old",
            "inspect",
            {"role": "user", "content": "inspect", "images": images},
            "done",
            [],
        )
        session.add(Task(id="images-next", conversation_id="images", goal="继续"))

    async def execute(self, request, **kwargs):
        assert request.messages == []
        assert request.images == []
        context = json.loads(request.prompt[request.prompt.index('{"executionEnvironment"') :])
        checkpoint = context["contextCheckpoint"]
        assert checkpoint["historicalImages"] == images
        assert "未加载图片" in checkpoint["imageNote"]
        archived = json.loads(Path(checkpoint["archivePath"]).read_text())
        assert archived["messages"][0]["images"] == images
        return AgentHostResult(
            output=complete(), input_message={"role": "user", "content": request.prompt}
        )

    monkeypatch.setattr(AgentHost, "execute", execute)
    assert await TaskDriver().poll_once()
    async with SessionLocal() as session:
        task = await session.get(Task, "images-next")
        assert task.status == "succeeded", task.error_json
        context = await load_context(session, "images")
        assert len(context.exchanges) == 1 and context.exchanges[0].is_checkpoint
        assert not any(message.get("images") for message in context.messages)
        assert (await load_context(session, "images", after_id=0)).messages[0]["images"] == images
