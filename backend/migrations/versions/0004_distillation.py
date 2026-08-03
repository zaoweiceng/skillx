"""Add conversation runs and distillation drafts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03

固定定义，不含后续 revision 追加的列（run_type 见 0006）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("trajectory_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_runs_conversation_id", "conversation_runs", ["conversation_id"]
    )
    op.create_table(
        "distillations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_run_id",
            sa.String(length=36),
            sa.ForeignKey("conversation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "draft_workflow_id",
            sa.String(length=36),
            sa.ForeignKey("workflows.id"),
            nullable=True,
        ),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_distillations_conversation_run_id", "distillations", ["conversation_run_id"]
    )


def downgrade() -> None:
    for table in ("distillations", "conversation_runs"):
        op.drop_table(table)
