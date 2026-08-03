"""Add conversation runs and distillation drafts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-03
"""

from alembic import op

from app.infrastructure.database import Base

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("conversation_runs", "distillations"):
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("distillations", "conversation_runs"):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
