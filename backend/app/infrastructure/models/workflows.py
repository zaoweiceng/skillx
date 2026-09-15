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
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    draft_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    draft_base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    draft_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    versions: Mapped[list[WorkflowVersion]] = relationship(back_populates="workflow")


class WorkflowVersion(Base, TimestampMixin):
    __tablename__ = "workflow_versions"
    __table_args__ = (Index("uq_workflow_version", "workflow_id", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80), index=True)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_from_draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_from_draft_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revision_lineage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    presentation_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class Component(Base, TimestampMixin):
    __tablename__ = "components"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"))
    default_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_versions.id"))
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    execution_class: Mapped[str] = mapped_column(String(20), default="zero_llm")
    seed_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    managed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class WorkflowCapabilityManifest(Base, TimestampMixin):
    __tablename__ = "workflow_capability_manifests"
    __table_args__ = (
        Index("uq_workflow_capability_version", "workflow_version_id", unique=True),
        Index("ix_workflow_capability_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE")
    )
    workflow_digest: Mapped[str] = mapped_column(String(80))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="published")
    search_text: Mapped[str] = mapped_column(Text)


class WorkflowFragmentManifest(Base, TimestampMixin):
    __tablename__ = "workflow_fragment_manifests"
    __table_args__ = (
        Index("uq_workflow_fragment_ref", "fragment_ref", unique=True),
        Index("ix_workflow_fragment_version", "workflow_version_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE")
    )
    fragment_ref: Mapped[str] = mapped_column(String(240))
    source_workflow_digest: Mapped[str] = mapped_column(String(80))
    closure_digest: Mapped[str] = mapped_column(String(80))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="published")


class PlanSnapshot(Base, TimestampMixin):
    __tablename__ = "plan_snapshots"
    __table_args__ = (Index("ix_plan_signature", "plan_signature"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80), index=True)
    source_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    locks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    source_map_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by_conversation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")
    plan_signature: Mapped[str] = mapped_column(String(80))


class WorkflowDisclosureSession(Base, TimestampMixin):
    __tablename__ = "workflow_disclosure_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    disclosed_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    call_budget_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)


class WorkflowDisclosureCall(Base, TimestampMixin):
    __tablename__ = "workflow_disclosure_calls"
    __table_args__ = (Index("ix_disclosure_call_session", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_disclosure_sessions.id", ondelete="CASCADE")
    )
    operation: Mapped[str] = mapped_column(String(32))
    request_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    response_digest: Mapped[str] = mapped_column(String(80))
    disclosure_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class PendingSelection(Base, TimestampMixin):
    __tablename__ = "pending_selections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    disclosure_session_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_disclosure_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    original_task: Mapped[str] = mapped_column(Text)
    selection_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    bindings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    missing_inputs_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="awaiting_input", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
