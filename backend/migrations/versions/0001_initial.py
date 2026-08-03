"""Create the initial workflow platform schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-03
"""

from alembic import op

from app.infrastructure.database import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
