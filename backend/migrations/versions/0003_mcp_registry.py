"""Add MCP registry and immutable tool snapshots.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03
"""

from alembic import op

from app.infrastructure.database import Base

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("mcp_servers", "mcp_server_versions", "mcp_tool_snapshots"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("mcp_tool_snapshots", "mcp_server_versions", "mcp_servers"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
