"""Atomic recovery claim leases, immutable substitutions and recovery node runs.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-04

006-A/B：把恢复从“读取 pending 行后直接调用 Agent”改为原子领取 + 唯一 Recovery
NodeRun。ExecutionCheckpoint 增加租约列（lease_owner/lease_expires_at/heartbeat_at）与
``(failed_node_run_id, attempt)`` 唯一约束，保证多个 Worker 只领取一次。恢复不再覆盖
失败 NodeRun：新增 ``output_substitutions`` 记录恢复提供的替代输出，原失败 attempt 永远
不可变。``recovery_node_run_id`` 关联该次恢复实际执行的 Recovery NodeRun。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_checkpoints",
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "execution_checkpoints",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_checkpoints",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_checkpoints",
        sa.Column("recovery_node_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "execution_checkpoints",
        sa.Column("approval_status", sa.String(length=20), nullable=False, server_default="none"),
    )
    # 同一失败 attempt 只能有一个 checkpoint：数据库级唯一约束，先查后插的竞态被消除。
    op.create_index(
        "uq_checkpoint_failed_attempt",
        "execution_checkpoints",
        ["failed_node_run_id", "attempt"],
        unique=True,
    )
    op.create_table(
        "output_substitutions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("checkpoint_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        # 被替代的失败 attempt（保持不可变，仅作溯源引用）。
        sa.Column("failed_node_run_id", sa.String(length=36), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="recovery"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_output_substitution_node",
        "output_substitutions",
        ["run_id", "node_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_output_substitution_node", table_name="output_substitutions")
    op.drop_table("output_substitutions")
    op.drop_index("uq_checkpoint_failed_attempt", table_name="execution_checkpoints")
    op.drop_column("execution_checkpoints", "approval_status")
    op.drop_column("execution_checkpoints", "recovery_node_run_id")
    op.drop_column("execution_checkpoints", "heartbeat_at")
    op.drop_column("execution_checkpoints", "lease_expires_at")
    op.drop_column("execution_checkpoints", "lease_owner")
