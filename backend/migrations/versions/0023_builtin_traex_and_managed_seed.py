"""Add builtin TraeX audit identity and managed seed metadata.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("components") as batch:
        batch.add_column(sa.Column("seed_key", sa.String(120), nullable=True))
        batch.add_column(sa.Column("managed_revision", sa.Integer(), nullable=True))
        batch.create_unique_constraint("uq_components_seed_key", ["seed_key"])
    with op.batch_alter_table("model_usage") as batch:
        batch.add_column(sa.Column("protocol", sa.String(80), nullable=True))
        batch.add_column(sa.Column("runtime_type", sa.String(40), nullable=True))
        batch.add_column(sa.Column("adapter_version", sa.String(80), nullable=True))
        batch.add_column(sa.Column("adapter_digest", sa.String(80), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_usage") as batch:
        batch.drop_column("adapter_digest")
        batch.drop_column("adapter_version")
        batch.drop_column("runtime_type")
        batch.drop_column("protocol")
    with op.batch_alter_table("components") as batch:
        batch.drop_constraint("uq_components_seed_key", type_="unique")
        batch.drop_column("managed_revision")
        batch.drop_column("seed_key")
