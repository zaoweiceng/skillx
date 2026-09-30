from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.payloads import ArtifactJSON

from .base import Base, TimestampMixin, utcnow


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_activity", "last_message_at", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    bound_component_id: Mapped[str | None] = mapped_column(
        ForeignKey("components.id"), nullable=True
    )
    agent_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    working_directory: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    traex_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workflow_mode: Mapped[str] = mapped_column(String(16), default="enabled")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationTurn(Base, TimestampMixin):
    __tablename__ = "conversation_turns"
    __table_args__ = (
        Index("ix_conversation_turns_conversation", "conversation_id", "accepted_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    user_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    assistant_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    parent_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True
    )
    workflow_mode_snapshot: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="accepted", index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ConversationRun(Base, TimestampMixin):
    __tablename__ = "conversation_runs"
    __table_args__ = (
        Index("ix_conversation_runs_claim", "status", "lease_expires_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    run_type: Mapped[str] = mapped_column(String(24), default="agent_run")
    input_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    output_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    trajectory_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    # 长任务持久化状态机：queued → running → succeeded|failed|cancelled。
    # 存在、状态与事件都不依赖 HTTP 连接或进程内存，可跨进程恢复。
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    next_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ConversationRunEvent(Base):
    """通用 Agent 运行的规范化事件流。

    与工作流 RunEvent 分离：这里记录 AgentRunService 归一化后的 plan/tool/step/
    终态事件，供轨迹抽屉订阅并在页面刷新后恢复状态，不依赖前端本地 state。
    """

    __tablename__ = "conversation_run_events"
    __table_args__ = (Index("uq_conversation_run_seq", "conversation_run_id", "seq", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_run_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Distillation(Base, TimestampMixin):
    __tablename__ = "distillations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_run_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    draft_workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"), nullable=True)
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON)
