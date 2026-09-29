"""Exercise the retained progressive selection service independently of Chat's task API."""

from uuid import uuid4

from fastapi import HTTPException

from app.application.product_history import turn_view
from app.infrastructure.database import Conversation, ConversationTurn, Message, utcnow


async def selection_message(conversation_id, body, session):
    from app.api.routes import _unbound_conversation_message

    conversation = await session.get(Conversation, conversation_id)
    accepted = utcnow()
    turn = ConversationTurn(
        id=str(uuid4()),
        conversation_id=conversation_id,
        workflow_mode_snapshot="enabled",
        status="processing",
        accepted_at=accepted,
    )
    session.add(turn)
    await session.flush()
    user = Message(
        id=str(uuid4()),
        conversation_id=conversation_id,
        turn_id=turn.id,
        role="user",
        content_json={"type": "text", "text": body.content},
    )
    session.add(user)
    turn.user_message_id = user.id
    conversation.title = body.content[:32]
    await session.commit()
    try:
        content, _ = await _unbound_conversation_message(
            session, conversation, body, turn.id, user.id
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        content = {"type": "workflow_selection", "state": "error", **detail}
    assistant = Message(
        id=str(uuid4()),
        conversation_id=conversation_id,
        turn_id=turn.id,
        role="assistant",
        content_json=content,
    )
    session.add(assistant)
    turn.assistant_message_id = assistant.id
    turn.first_response_at = utcnow()
    if content.get("state") in {"error", "ask_user", "unavailable"}:
        turn.status = content["state"]
        turn.completed_at = utcnow()
    await session.commit()
    return {"userMessageId": user.id, "assistant": content, "turn": await turn_view(session, turn)}
