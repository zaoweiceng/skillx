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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


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


class RuntimeProfile(Base, TimestampMixin):
    __tablename__ = "runtime_profiles"
    __table_args__ = (
        Index(
            "uq_runtime_profile_default", "kind", unique=True, sqlite_where=text("is_default = 1")
        ),
        CheckConstraint("kind IN ('model', 'agent')", name="ck_runtime_profile_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(120))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    probe_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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
