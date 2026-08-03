"""Add environment-backed Secret references.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-03
"""

from alembic import op

from app.infrastructure.database import Base

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["secret_references"].create(
        bind=op.get_bind(),
        checkfirst=True,
    )


def downgrade() -> None:
    Base.metadata.tables["secret_references"].drop(
        bind=op.get_bind(),
        checkfirst=True,
    )
