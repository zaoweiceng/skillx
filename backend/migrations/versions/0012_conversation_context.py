"""Persist append-only conversation model context."""

import sqlalchemy as sa
from alembic import op

revision = "0012_conversation_context"
down_revision = "0011_workspace_membership"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "conversation_context_exchanges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("decision_key", sa.String(160), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("is_checkpoint", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "decision_key", name="uq_context_decision"),
    )
    op.create_index(
        "ix_context_conversation_order", "conversation_context_exchanges", ["conversation_id", "id"]
    )
    op.create_index(
        "ix_conversation_context_exchanges_task_id", "conversation_context_exchanges", ["task_id"]
    )


def downgrade():
    op.drop_table("conversation_context_exchanges")
