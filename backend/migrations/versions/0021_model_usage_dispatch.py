"""Record attempted, dispatched and completed model-call facts.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("attempted_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column("request_dispatched_at", sa.DateTime(timezone=True))
        )
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE model_usage SET attempted_at = created_at")
    op.execute(
        "UPDATE model_usage "
        "SET request_dispatched_at = created_at, completed_at = created_at "
        "WHERE usage_complete = 1 OR total_tokens IS NOT NULL"
    )
    with op.batch_alter_table("model_usage") as batch:
        batch.alter_column("attempted_at", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("completed_at")
        batch.drop_column("request_dispatched_at")
        batch.drop_column("attempted_at")
