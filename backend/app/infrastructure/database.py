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
    execution_class: Mapped[str] = mapped_column(String(20), default="zero_llm")


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


class SecretReference(Base, TimestampMixin):
    __tablename__ = "secret_references"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    provider: Mapped[str] = mapped_column(String(20), default="env")
    external_key: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")


class MCPServer(Base, TimestampMixin):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    transport: Mapped[str] = mapped_column(String(20))
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MCPServerVersion(Base, TimestampMixin):
    __tablename__ = "mcp_server_versions"
    __table_args__ = (Index("uq_mcp_server_version", "server_id", "version", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="published")


class MCPToolSnapshot(Base, TimestampMixin):
    __tablename__ = "mcp_tool_snapshots"
    __table_args__ = (Index("uq_mcp_tool_name", "server_version_id", "name", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_version_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_server_versions.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_schema_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    digest: Mapped[str] = mapped_column(String(80))
    side_effect: Mapped[str] = mapped_column(String(30), default="none")
    idempotent: Mapped[bool] = mapped_column(default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # effect 分类的可信来源与审核状态：未知 Tool 不得被默认当作安全读操作。
    effect_source: Mapped[str] = mapped_column(String(20), default="unknown")
    effect_review_status: Mapped[str] = mapped_column(String(20), default="unreviewed")


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


class ConversationRun(Base, TimestampMixin):
    __tablename__ = "conversation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    run_type: Mapped[str] = mapped_column(String(24), default="agent_run")
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    trajectory_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


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
