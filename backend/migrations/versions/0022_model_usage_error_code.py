"""Add explicit model usage error code.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("error_code", sa.String(80)))


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("error_code")
