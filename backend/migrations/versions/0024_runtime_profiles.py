"""Persist model and agent profiles and conversation selections."""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.CheckConstraint("kind IN ('model', 'agent')", name="ck_runtime_profile_kind"),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("probe_json", sa.JSON(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_profiles_kind", "runtime_profiles", ["kind"])
    op.create_index(
        "uq_runtime_profile_default",
        "runtime_profiles",
        ["kind"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("agent_profile_id", sa.String(36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("agent_profile_id")
    op.drop_table("runtime_profiles")
