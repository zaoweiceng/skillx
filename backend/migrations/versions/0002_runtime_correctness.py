"""Add atomic run event sequence.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    run_columns = {item["name"] for item in inspector.get_columns("runs")}
    if "next_event_seq" not in run_columns:
        op.add_column(
            "runs",
            sa.Column("next_event_seq", sa.Integer(), nullable=False, server_default="0"),
        )
    component_columns = {item["name"] for item in inspector.get_columns("components")}
    if "execution_class" not in component_columns:
        op.add_column(
            "components",
            sa.Column(
                "execution_class",
                sa.String(length=20),
                nullable=False,
                server_default="zero_llm",
            ),
        )


def downgrade() -> None:
    op.drop_column("components", "execution_class")
    op.drop_column("runs", "next_event_seq")
