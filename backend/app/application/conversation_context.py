"""Durable model history. Callers own transaction and context-budget policy."""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import ConversationContextExchange, Task


@dataclass(frozen=True)
class ConversationContext:
    exchanges: list[ConversationContextExchange]
    messages: list[dict[str, Any]]
    evidence: list[dict[str, Any]]


async def load_context(
    session: AsyncSession, conversation_id: str, *, after_id: int | None = None
) -> ConversationContext:
    if after_id is None:
        checkpoint_id = await session.scalar(
            select(func.max(ConversationContextExchange.id)).where(
                ConversationContextExchange.conversation_id == conversation_id,
                ConversationContextExchange.is_checkpoint.is_(True),
            )
        )
        after_id = (checkpoint_id - 1) if checkpoint_id else 0
    exchanges = list(
        (
            await session.scalars(
                select(ConversationContextExchange)
                .where(
                    ConversationContextExchange.conversation_id == conversation_id,
                    ConversationContextExchange.id > after_id,
                )
                .order_by(ConversationContextExchange.id)
            )
        ).all()
    )
    messages = []
    evidence = []
    positions = {}
    for exchange in exchanges:
        messages.extend(
            [
                exchange.request,
                {"role": "assistant", "content": exchange.response},
            ]
        )
        for record in exchange.evidence_json:
            key = (
                str(record["id"])
                if "id" in record
                else json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
            if key in positions:
                evidence[positions[key]] = record
            else:
                positions[key] = len(evidence)
                evidence.append(record)
    return ConversationContext(exchanges=exchanges, messages=messages, evidence=evidence)


async def append_exchange(
    session: AsyncSession,
    conversation_id: str,
    task_id: str,
    decision_key: str,
    request: dict[str, Any],
    response: str,
    evidence: list[dict[str, Any]],
    *,
    is_checkpoint: bool = False,
) -> ConversationContextExchange:
    """Append an exchange atomically with caller state; retries cannot rewrite it.

    Evidence records must have been observed in this request or its saved history,
    never speculative or merely available task records. Checkpoints may retain
    lightweight references while the original exchanges keep their full bodies.
    """
    task = await session.get(Task, task_id)
    if task is None or task.conversation_id != conversation_id:
        raise ValueError("Context task does not belong to conversation")
    if not decision_key or len(decision_key) > 160:
        raise ValueError("Context decision_key must contain 1–160 characters")
    existing = await session.scalar(
        select(ConversationContextExchange).where(
            ConversationContextExchange.conversation_id == conversation_id,
            ConversationContextExchange.decision_key == decision_key,
        )
    )
    if existing is not None:
        if (
            existing.task_id,
            existing.request,
            existing.response,
            existing.evidence_json,
            existing.is_checkpoint,
        ) != (task_id, request, response, evidence, is_checkpoint):
            raise ValueError("Context decision_key already contains a different exchange")
        return existing
    exchange = ConversationContextExchange(
        conversation_id=conversation_id,
        task_id=task_id,
        decision_key=decision_key,
        request=request,
        response=response,
        evidence_json=evidence,
        is_checkpoint=is_checkpoint,
    )
    session.add(exchange)
    await session.flush()
    return exchange
