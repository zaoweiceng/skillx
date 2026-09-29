"""Durable goal execution, human input and capability invocations."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.payloads import ArtifactJSON

from .base import Base, TimestampMixin


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_turns.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    goal: Mapped[str] = mapped_column(String)
    state_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON, default=dict)
    runtime_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON, default=dict)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    decisions: Mapped[int] = mapped_column(Integer, default=0)
    active_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskEvent(Base, TimestampMixin):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON, default=dict)


class Invocation(Base, TimestampMixin):
    __tablename__ = "invocations"
    __table_args__ = (UniqueConstraint("scope", "call_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    scope: Mapped[str] = mapped_column(String(80))
    call_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    runtime_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON, default=dict)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HumanInteraction(Base, TimestampMixin):
    __tablename__ = "human_interactions"
    __table_args__ = (UniqueConstraint("task_id", "call_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    call_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    request_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    snapshot_digest: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    answer_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON)
    answer_key: Mapped[str | None] = mapped_column(String(160))


class LearningJob(Base, TimestampMixin):
    __tablename__ = "learning_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str | None] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    result_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
