import pytest

from app.application.conversation_context import append_exchange, load_context
from app.infrastructure.database import SessionLocal
from app.infrastructure.models import Conversation, Task


async def seed(session):
    session.add_all([Conversation(id="a"), Conversation(id="b")])
    await session.flush()
    session.add_all(
        [
            Task(id="one", conversation_id="a", goal="first"),
            Task(id="two", conversation_id="a", goal="next"),
            Task(id="other", conversation_id="b", goal="other"),
        ]
    )
    await session.flush()


async def test_context_persists_across_tasks_and_preserves_images():
    request = {"role": "user", "content": "inspect", "images": [{"url": "image"}]}
    evidence = [{"id": "result", "status": "succeeded", "output": "evidence"}]
    async with SessionLocal() as session, session.begin():
        await seed(session)
        first = await append_exchange(session, "a", "one", "one:1", request, "answer", evidence)
        first_id = first.id
    async with SessionLocal() as session, session.begin():
        await append_exchange(
            session, "a", "two", "two:1", {"role": "user", "content": "continue"}, "done", evidence
        )
        context = await load_context(session, "a")
        assert [item.task_id for item in context.exchanges] == ["one", "two"]
        assert context.messages[0] == request
        assert context.evidence == evidence
        assert (await load_context(session, "b")).messages == []
        assert len((await load_context(session, "a", after_id=first_id)).exchanges) == 1


async def test_context_retry_is_idempotent_and_cannot_cross_conversations():
    async with SessionLocal() as session, session.begin():
        await seed(session)
        args = (session, "a", "one", "decision", {"role": "user", "content": "hi"}, "hello", [])
        first = await append_exchange(*args)
        assert (await append_exchange(*args)).id == first.id
        with pytest.raises(ValueError, match="different exchange"):
            await append_exchange(
                session, "a", "one", "decision", {"role": "user", "content": "changed"}, "hello", []
            )
        with pytest.raises(ValueError, match="does not belong"):
            await append_exchange(session, "a", "other", "decision2", {}, "", [])
        assert len((await load_context(session, "a")).exchanges) == 1


async def test_checkpoint_retains_originals_and_rollback_is_atomic():
    async with SessionLocal() as session, session.begin():
        await seed(session)
        await append_exchange(
            session,
            "a",
            "one",
            "old",
            {"role": "user", "content": "large original"},
            "old answer",
            [],
        )
        checkpoint = await append_exchange(
            session,
            "a",
            "two",
            "checkpoint",
            {"role": "user", "content": "summary"},
            "new answer",
            [],
            is_checkpoint=True,
        )
        assert [item.id for item in (await load_context(session, "a")).exchanges] == [checkpoint.id]
        assert len((await load_context(session, "a", after_id=0)).exchanges) == 2
    async with SessionLocal() as session:
        await append_exchange(session, "a", "two", "rollback", {}, "discard", [])
        await session.rollback()
    async with SessionLocal() as session:
        assert len((await load_context(session, "a", after_id=0)).exchanges) == 2
