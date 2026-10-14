"""Persist capability availability independently of immutable versions."""
import sqlalchemy as sa
from alembic import op
revision = "0013_capability_availability"
down_revision = "0012_conversation_context"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("capability_availability",
        sa.Column("capability_id", sa.String(36), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))

def downgrade():
    op.drop_table("capability_availability")
