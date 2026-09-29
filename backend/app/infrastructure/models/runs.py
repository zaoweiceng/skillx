from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.payloads import ArtifactJSON

from .base import Base, TimestampMixin, utcnow


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
            name="ck_run_execution_source",
        ),
        Index("ix_runs_component_created", "component_id", "created_at", "id"),
        Index(
            "ix_runs_workflow_version_created",
            "workflow_version_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("workflow_versions.id"), nullable=True
    )
    plan_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("plan_snapshots.id"), nullable=True
    )
    component_id: Mapped[str | None] = mapped_column(ForeignKey("components.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    input_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    runtime_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeRun(Base, TimestampMixin):
    __tablename__ = "node_runs"
    __table_args__ = (
        Index("uq_node_attempt", "run_id", "node_id", "attempt", unique=True),
        Index("ix_node_claim", "status", "available_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(64))
    node_type: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), index=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("uq_run_seq", "run_id", "seq", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("node_runs.id", ondelete="SET NULL"), nullable=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80))
    payload_json: Mapped[dict[str, Any]] = mapped_column(ArtifactJSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_run_id: Mapped[str] = mapped_column(ForeignKey("node_runs.id", ondelete="CASCADE"))
    node_id: Mapped[str] = mapped_column(String(64))
    snapshot_digest: Mapped[str] = mapped_column(String(80))
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "(run_id IS NOT NULL) != (conversation_run_id IS NOT NULL)",
            name="ck_artifact_source",
        ),
        Index(
            "uq_conversation_artifact_name",
            "conversation_run_id",
            "kind",
            "name",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=True
    )
    conversation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255))
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RunPresentation(Base, TimestampMixin):
    __tablename__ = "run_presentations"
    __table_args__ = (Index("uq_run_presentation_revision", "run_id", "revision", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    producer: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_template_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(ArtifactJSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        Index("uq_model_usage_call_attempt", "call_id", "attempt", unique=True),
        Index("ix_model_usage_turn", "turn_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="CASCADE"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True
    )
    conversation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=True
    )
    presentation_id: Mapped[str | None] = mapped_column(
        ForeignKey("run_presentations.id", ondelete="CASCADE"), nullable=True
    )
    call_id: Mapped[str] = mapped_column(String(160))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    protocol: Mapped[str | None] = mapped_column(String(80), nullable=True)
    runtime_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    adapter_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    runtime_used: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stage: Mapped[str] = mapped_column(String(40))
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    request_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
