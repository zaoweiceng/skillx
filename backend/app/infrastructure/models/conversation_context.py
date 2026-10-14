"""Append-only model exchanges, scoped to their owning conversation."""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.payloads import ArtifactJSON

from .base import Base, utcnow


class ConversationContextExchange(Base):
    __tablename__ = "conversation_context_exchanges"
    __table_args__ = (
        UniqueConstraint("conversation_id", "decision_key", name="uq_context_decision"),
        Index("ix_context_conversation_order", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    decision_key: Mapped[str] = mapped_column(String(160))
    request: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    is_checkpoint: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str] = mapped_column(ArtifactJSON)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(ArtifactJSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
