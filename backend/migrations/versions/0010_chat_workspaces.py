"""Persist chat workspace directories independently of conversations."""
import sqlalchemy as sa
from alembic import op

revision = "0010_chat_workspaces"
down_revision = "0009_managed_skills"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_workspaces",
        sa.Column("path", sa.String(4096), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("chat_workspaces")
