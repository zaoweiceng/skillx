"""Split MCP tool remote-contract digest from local effect-review policy.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-04

005-B：MCPToolSnapshot 新增 ``contract_digest``——只覆盖远端 descriptor（name/
description/inputSchema/outputSchema/timeout），用于 discovery 侦测真实远端契约漂移。
原 ``digest`` 继续覆盖完整契约（含 effect/idempotent），作为发布 lock 的运行前核对键。
两者分离后，本地人工分类（只改 effect/idempotent）不会被再次 discovery 误判为远端漂移。
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_tool_snapshots",
        sa.Column("contract_digest", sa.String(length=80), nullable=False, server_default=""),
    )
    # 回填：已有行的 contract_digest 先置为其 digest（不影响后续新快照的精确计算）。
    op.execute("UPDATE mcp_tool_snapshots SET contract_digest = digest")


def downgrade() -> None:
    op.drop_column("mcp_tool_snapshots", "contract_digest")
