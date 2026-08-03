"""Add agent run service tables and MCP effect classification.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-03

新增：
- conversation_runs.error_json：记录服务端 Agent 运行失败原因。
- conversation_run_events：AgentRunService 归一化事件流，供轨迹抽屉恢复。
- mcp_tool_snapshots.effect_source / effect_review_status：Tool 副作用分类的
  可信来源与审核状态。未知 Tool 不得默认当作安全读操作。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_runs",
        sa.Column("error_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "conversation_run_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_run_id",
            sa.String(length=36),
            sa.ForeignKey("conversation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_run_events_conversation_run_id",
        "conversation_run_events",
        ["conversation_run_id"],
    )
    op.create_index(
        "uq_conversation_run_seq",
        "conversation_run_events",
        ["conversation_run_id", "seq"],
        unique=True,
    )
    op.add_column(
        "mcp_tool_snapshots",
        sa.Column(
            "effect_source",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "mcp_tool_snapshots",
        sa.Column(
            "effect_review_status",
            sa.String(length=20),
            nullable=False,
            server_default="unreviewed",
        ),
    )


def downgrade() -> None:
    op.drop_column("mcp_tool_snapshots", "effect_review_status")
    op.drop_column("mcp_tool_snapshots", "effect_source")
    op.drop_index(
        "uq_conversation_run_seq", table_name="conversation_run_events"
    )
    op.drop_index(
        "ix_conversation_run_events_conversation_run_id",
        table_name="conversation_run_events",
    )
    op.drop_table("conversation_run_events")
    op.drop_column("conversation_runs", "error_json")
