"""Persist working directory on conversations and recover prior task selections."""

import sqlalchemy as sa
from alembic import op

revision = "0007_conversation_directory"
down_revision = "0006_reasoning_effort"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("conversations", sa.Column("working_directory", sa.String(4096), nullable=True))
    op.execute("""
        UPDATE conversations SET working_directory = (
            SELECT json_extract(state_json, '$.workingDirectory') FROM tasks
            WHERE tasks.conversation_id = conversations.id
              AND json_type(state_json, '$.workingDirectory') = 'text'
            ORDER BY created_at DESC, id DESC LIMIT 1
        )
    """)


def downgrade():
    op.drop_column("conversations", "working_directory")
