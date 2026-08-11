from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
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
    event,
)
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


def rfc3339(value: datetime | None) -> str | None:
    """Serialize SQLite datetimes as explicit UTC RFC 3339 values."""

    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
    draft_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    draft_base_version_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
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
    published_from_draft_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    published_from_draft_digest: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    revision_lineage_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
    presentation_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
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
    seed_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True
    )
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
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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
    # 远端 descriptor 契约 digest（不含本地 effect/idempotent 审核），用于 discovery
    # 侦测真实远端漂移；本地人工分类只改 effect 审核字段时不改变它。
    contract_digest: Mapped[str] = mapped_column(String(80), default="")
    side_effect: Mapped[str] = mapped_column(String(30), default="none")
    idempotent: Mapped[bool] = mapped_column(default=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # effect 分类的可信来源与审核状态：未知 Tool 不得被默认当作安全读操作。
    effect_source: Mapped[str] = mapped_column(String(20), default="unknown")
    effect_review_status: Mapped[str] = mapped_column(String(20), default="unreviewed")


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


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_activity", "last_message_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(120), default="local-user")
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    bound_component_id: Mapped[str | None] = mapped_column(
        ForeignKey("components.id"), nullable=True
    )
    workflow_mode: Mapped[str] = mapped_column(String(16), default="enabled")
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON)
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
    user_message_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    assistant_message_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    parent_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversation_turns.id", ondelete="SET NULL"), nullable=True
    )
    workflow_mode_snapshot: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="accepted", index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_response_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    trajectory_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
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


class BrowserRuntimeHeartbeat(Base, TimestampMixin):
    """Browser Worker 的实际启动预检事实。

    API 只读取此表，不依据网络 allowlist 推断 Chromium 是否可启动。
    """

    __tablename__ = "browser_runtime_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    chromium_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    chromium_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    playwright_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    launch_succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Distillation(Base, TimestampMixin):
    __tablename__ = "distillations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_run_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_runs.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    draft_workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id"), nullable=True)
    analysis_json: Mapped[dict[str, Any]] = mapped_column(JSON)


class ExecutionCheckpoint(Base, TimestampMixin):
    """节点重试耗尽且命中 fallback 时冻结的恢复检查点。

    保存已完成输出/Artifact 引用、失败输入/标准错误、capability locks、剩余子图与
    已提交副作用；恢复只消费该持久化 checkpoint，不依赖原 Worker 内存。
    """

    __tablename__ = "execution_checkpoints"
    __table_args__ = (
        Index("ix_execution_checkpoints_run_id", "run_id"),
        Index(
            "uq_checkpoint_failed_attempt",
            "failed_node_run_id",
            "attempt",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    workflow_version_id: Mapped[str] = mapped_column(String(36))
    failed_node_id: Mapped[str] = mapped_column(String(64))
    failed_node_run_id: Mapped[str] = mapped_column(String(36))
    attempt: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 原子领取租约：单条 CAS 把 pending→running 并写入 owner/expiry/heartbeat，未领取成功
    # 的 Worker 不得调用 Agent。
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 该次恢复实际执行的 Recovery NodeRun（唯一）。
    recovery_node_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 审批型 fallback：none|pending|approved|rejected。
    approval_status: Mapped[str] = mapped_column(String(20), default="none")


class OutputSubstitution(Base, TimestampMixin):
    """恢复提供的替代输出（不覆盖失败 NodeRun）。

    原失败 attempt 永远不可变；恢复从 checkpoint 剩余子图续跑时，下游节点显式从这里读取
    被替代节点的输出。每个 (run_id, node_id) 至多一条替代。
    """

    __tablename__ = "output_substitutions"
    __table_args__ = (
        Index("uq_output_substitution_node", "run_id", "node_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    checkpoint_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(64))
    failed_node_run_id: Mapped[str] = mapped_column(String(36))
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(40), default="recovery")


class FailureObservation(Base, TimestampMixin):
    """按稳定 fingerprint 聚合的失败观测（受控 WorkflowPatch 的资格依据）。

    瞬态错误默认只记录并累加 occurrence；只有可复现的非瞬态 fingerprint 才允许生成 Patch
    proposal。资格由后端事实（occurrence + classification）计算，不接受前端自报。
    """

    __tablename__ = "failure_observations"
    __table_args__ = (
        CheckConstraint(
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
            name="ck_failure_observation_source",
        ),
        Index("uq_failure_observation_fingerprint", "fingerprint", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(80))
    workflow_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_node_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_id: Mapped[str] = mapped_column(String(64))
    node_type: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(32))
    occurrence: Mapped[int] = mapped_column(Integer, default=1)
    recovered: Mapped[bool] = mapped_column(default=False)
    baseline_reproduced: Mapped[bool] = mapped_column(default=False)
    evidence_kind: Mapped[str] = mapped_column(
        String(40), default="unsupported"
    )
    reproduction_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict
    )
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    locks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    contract_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recovered_outcome_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


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
    __table_args__ = (
        Index("ix_disclosure_call_session", "session_id", "created_at"),
    )

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


class RunPresentation(Base, TimestampMixin):
    __tablename__ = "run_presentations"
    __table_args__ = (
        Index("uq_run_presentation_revision", "run_id", "revision", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    producer: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    config_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_template_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    request_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskCheckpoint(Base, TimestampMixin):
    __tablename__ = "task_checkpoints"
    __table_args__ = (
        Index("ix_task_checkpoint_source", "source_kind", "source_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str] = mapped_column(String(36))
    source_digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80))
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)


class RecoveryAttempt(Base, TimestampMixin):
    __tablename__ = "recovery_attempts"
    __table_args__ = (
        Index("uq_recovery_attempt", "checkpoint_id", "attempt", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("execution_checkpoints.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(Integer)
    plan_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))


class ReplayEvidence(Base, TimestampMixin):
    __tablename__ = "replay_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("failure_observations.id", ondelete="CASCADE"), index=True
    )
    baseline_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    patched_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(default=False)
    fixture_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    patch_of_observation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_definition_digest: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    patch_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    patched_definition_digest: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    human_patch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class RevisionProposal(Base, TimestampMixin):
    __tablename__ = "revision_proposals"
    __table_args__ = (
        Index(
            "uq_revision_proposal_content",
            "observation_id",
            "baseline_digest",
            "proposal_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("failure_observations.id", ondelete="CASCADE"), index=True
    )
    base_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    fixture_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    baseline_digest: Mapped[str] = mapped_column(String(80))
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    proposal_digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="proposed")


class HumanPatch(Base, TimestampMixin):
    __tablename__ = "human_patches"
    __table_args__ = (
        Index(
            "uq_human_patch_content",
            "parent_proposal_id",
            "patch_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("revision_proposals.id", ondelete="CASCADE"), index=True
    )
    editor_id: Mapped[str] = mapped_column(String(120), default="local-user")
    patch_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    patch_digest: Mapped[str] = mapped_column(String(80))


class ReplayFixture(Base, TimestampMixin):
    __tablename__ = "replay_fixtures"
    __table_args__ = (
        Index("uq_replay_fixture_node_run", "node_run_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    node_run_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE")
    )
    source_digest: Mapped[str] = mapped_column(String(80))
    input_digest: Mapped[str] = mapped_column(String(80))
    output_digest: Mapped[str] = mapped_column(String(80))
    capture_policy: Mapped[str] = mapped_column(String(40))
    evidence_kind: Mapped[str] = mapped_column(
        String(40), default="unsupported"
    )


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
