"""Add MCP registry and immutable tool snapshots.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column("current_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "server_id",
            sa.String(length=36),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_server_versions_server_id", "mcp_server_versions", ["server_id"])
    op.create_index(
        "uq_mcp_server_version", "mcp_server_versions", ["server_id", "version"], unique=True
    )
    op.create_table(
        "mcp_tool_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "server_version_id",
            sa.String(length=36),
            sa.ForeignKey("mcp_server_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.JSON(), nullable=False),
        sa.Column("output_schema_json", sa.JSON(), nullable=True),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("side_effect", sa.String(length=30), nullable=False),
        sa.Column("idempotent", sa.Boolean(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mcp_tool_snapshots_server_version_id", "mcp_tool_snapshots", ["server_version_id"]
    )
    op.create_index(
        "uq_mcp_tool_name", "mcp_tool_snapshots", ["server_version_id", "name"], unique=True
    )


def downgrade() -> None:
    for table in ("mcp_tool_snapshots", "mcp_server_versions", "mcp_servers"):
        op.drop_table(table)
