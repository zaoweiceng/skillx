"""Manage source Skills and their immutable SkillX conversions."""

import sqlalchemy as sa
from alembic import op

revision = "0009_managed_skills"
down_revision = "0008_pi_model_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "managed_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "skillx_capability_id",
            sa.String(36),
            sa.ForeignKey("capabilities.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "skillx_version_id",
            sa.String(36),
            sa.ForeignKey("capability_versions.id", ondelete="SET NULL"),
        ),
        sa.Column("converted_source_digest", sa.String(80)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("managed_skills")
