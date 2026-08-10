"""Split Agent and LLM Runtime audit facts.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column(
                "runtime_snapshot_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("runtime_used", sa.String(32)))
        batch.add_column(sa.Column("runtime_digest", sa.String(80)))
        batch.add_column(sa.Column("fallback_reason", sa.String(80)))


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("fallback_reason")
        batch.drop_column("runtime_digest")
        batch.drop_column("runtime_used")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("runtime_snapshot_json")
