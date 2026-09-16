"""Persist a conversation's TraeX model selection independently of shared profiles."""

import sqlalchemy as sa
from alembic import op

revision = "0002_traex_model"
down_revision = "0001_refactored"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("traex_model", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "traex_model")
