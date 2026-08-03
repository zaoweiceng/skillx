"""Add run_type to conversation runs.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_runs",
        sa.Column(
            "run_type",
            sa.String(length=24),
            nullable=False,
            server_default="agent_run",
        ),
    )


def downgrade() -> None:
    op.drop_column("conversation_runs", "run_type")
