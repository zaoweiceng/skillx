"""Add execution checkpoints and recovery runs, plus node fallback DSL storage.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-04

005-D：通用 ExecutionCheckpoint / RecoveryRun。任何节点重试耗尽且命中显式 fallback 时，
冻结已完成输出、剩余子图、失败事实、能力锁与最小安全上下文，交给受限 Agent 恢复；
恢复只消费持久化 checkpoint，不依赖原 Worker 内存。
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_checkpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("failed_node_id", sa.String(length=64), nullable=False),
        sa.Column("failed_node_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        # 冻结的已完成输出/Artifact 引用、失败输入/错误、能力锁、剩余子图、已提交副作用。
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_execution_checkpoints_run_id", "execution_checkpoints", ["run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_checkpoints_run_id", table_name="execution_checkpoints")
    op.drop_table("execution_checkpoints")
