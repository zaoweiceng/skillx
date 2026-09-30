"""Persist conversation reasoning effort."""

import sqlalchemy as sa
from alembic import op

revision = "0006_reasoning_effort"
down_revision = "0005_conversation_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("reasoning_effort", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("conversations", "reasoning_effort")
