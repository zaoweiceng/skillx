"""Persist conversation history deletion."""

import sqlalchemy as sa
from alembic import op

revision = "0005_conversation_history"
down_revision = "0004_learning_faults"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "conversations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade():
    op.drop_column("conversations", "deleted_at")
