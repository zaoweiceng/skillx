"""Persist conversation agent runs as recoverable long tasks.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-03

004-A：把 Conversation Agent 从“API 事务内同步执行”改为“先持久化 queued run，
再由独立 Worker 领取执行”的长任务。ConversationRun 因此需要状态机时间戳、租约、
心跳、取消请求、attempt 与原子事件序号列。
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_runs",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("next_event_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "conversation_runs",
        sa.Column("config_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_conversation_runs_claim",
        "conversation_runs",
        ["status", "lease_expires_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_runs_claim", table_name="conversation_runs")
    op.drop_column("conversation_runs", "config_json")
    op.drop_column("conversation_runs", "next_event_seq")
    op.drop_column("conversation_runs", "attempt")
    op.drop_column("conversation_runs", "cancellation_requested_at")
    op.drop_column("conversation_runs", "heartbeat_at")
    op.drop_column("conversation_runs", "lease_expires_at")
    op.drop_column("conversation_runs", "lease_owner")
    op.drop_column("conversation_runs", "finished_at")
    op.drop_column("conversation_runs", "started_at")
    op.drop_column("conversation_runs", "queued_at")
