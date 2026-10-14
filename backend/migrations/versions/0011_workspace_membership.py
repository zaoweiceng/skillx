"""Separate sidebar workspace membership from execution directories."""

import sqlalchemy as sa
from alembic import op

revision = "0011_workspace_membership"
down_revision = "0010_chat_workspaces"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_workspaces", sa.Column("name", sa.String(200), nullable=True))
    op.add_column("conversations", sa.Column("workspace_path", sa.String(4096), nullable=True))
    op.create_index("ix_conversations_workspace_path", "conversations", ["workspace_path"])
    # Preserve groups previously inferred from existing conversations exactly once.
    op.execute("""INSERT INTO chat_workspaces (path, created_at, updated_at)
        SELECT DISTINCT working_directory, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM conversations WHERE deleted_at IS NULL AND working_directory IS NOT NULL
        AND working_directory NOT IN (SELECT path FROM chat_workspaces)""")
    op.execute("""UPDATE conversations SET workspace_path = working_directory
        WHERE deleted_at IS NULL AND working_directory IS NOT NULL""")


def downgrade():
    op.drop_index("ix_conversations_workspace_path", table_name="conversations")
    op.drop_column("conversations", "workspace_path")
    op.drop_column("chat_workspaces", "name")
