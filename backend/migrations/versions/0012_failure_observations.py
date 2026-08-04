"""Persist failure observations for reproducibility-gated WorkflowPatch.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-04

006-D-01：把失败可复现性接入生产路径。每次可归因的节点失败按稳定 fingerprint 聚合为
FailureObservation：记录来源 run/checkpoint、分类、错误码、契约摘要、恢复结果与出现次数。
瞬态错误默认只记录；只有同版本/节点/锁的稳定 fingerprint 达到复现阈值才允许生成
WorkflowPatch proposal——资格由后端事实计算，不接受前端自报 reproducible。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "failure_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("fingerprint", sa.String(length=80), nullable=False),
        sa.Column("workflow_version_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("occurrence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recovered", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_run_id", sa.String(length=36), nullable=True),
        sa.Column("last_checkpoint_id", sa.String(length=36), nullable=True),
        sa.Column("contract_summary_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_failure_observation_fingerprint",
        "failure_observations",
        ["fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_failure_observation_fingerprint", table_name="failure_observations")
    op.drop_table("failure_observations")
