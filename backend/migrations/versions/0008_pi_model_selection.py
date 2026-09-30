"""Persist Pi service/model choices independently of the Agent profile."""

import sqlalchemy as sa
from alembic import op

revision = "0008_pi_model_selection"
down_revision = "0007_conversation_directory"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("model_profile_id", sa.String(36), nullable=True))
    op.add_column("conversations", sa.Column("agent_model", sa.String(200), nullable=True))


def downgrade():
    op.drop_column("conversations", "agent_model")
    op.drop_column("conversations", "model_profile_id")
