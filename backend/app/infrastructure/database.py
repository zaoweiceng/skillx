from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Workflow(Base, TimestampMixin):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON)
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
    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class Component(Base, TimestampMixin):
    __tablename__ = "components"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"))
    default_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_versions.id"))
    visibility: Mapped[str] = mapped_column(String(20), default="private")


class Run(Base, TimestampMixin):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_version_id: Mapped[str] = mapped_column(ForeignKey("workflow_versions.id"))
    component_id: Mapped[str | None] = mapped_column(ForeignKey("components.id"), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
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


class Capability(Base, TimestampMixin):
    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class CapabilityVersion(Base, TimestampMixin):
    __tablename__ = "capability_versions"
    __table_args__ = (Index("uq_capability_version", "capability_id", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    capability_id: Mapped[str] = mapped_column(
        ForeignKey("capabilities.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(40))
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    package_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(255))
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    bound_component_id: Mapped[str | None] = mapped_column(
        ForeignKey("components.id"), nullable=True
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


settings = get_settings()
engine: AsyncEngine = create_async_engine(settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def configure_sqlite(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async def init_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
