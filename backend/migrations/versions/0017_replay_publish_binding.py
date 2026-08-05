"""Bind revision proposals and replay evidence to published snapshots.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revision_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "observation_id",
            sa.String(36),
            sa.ForeignKey("failure_observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "base_version_id",
            sa.String(36),
            sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_run_id", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("fixture_digest", sa.String(80), nullable=True),
        sa.Column("baseline_digest", sa.String(80), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("proposal_digest", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_revision_proposal_content",
        "revision_proposals",
        ["observation_id", "baseline_digest", "proposal_digest"],
        unique=True,
    )
    op.create_table(
        "human_patches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "parent_proposal_id",
            sa.String(36),
            sa.ForeignKey("revision_proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("editor_id", sa.String(120), nullable=False),
        sa.Column("patch_json", sa.JSON(), nullable=False),
        sa.Column("patch_digest", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_human_patch_content",
        "human_patches",
        ["parent_proposal_id", "patch_digest"],
        unique=True,
    )
    with op.batch_alter_table("replay_evidence") as batch:
        batch.add_column(sa.Column("base_definition_digest", sa.String(80)))
        batch.add_column(sa.Column("patch_digest", sa.String(80)))
        batch.add_column(sa.Column("patched_definition_digest", sa.String(80)))
        batch.add_column(sa.Column("proposal_id", sa.String(36)))
        batch.add_column(sa.Column("human_patch_id", sa.String(36)))
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(sa.Column("draft_digest", sa.String(80)))
    with op.batch_alter_table("workflow_versions") as batch:
        batch.add_column(sa.Column("published_from_draft_revision", sa.Integer()))
        batch.add_column(sa.Column("published_from_draft_digest", sa.String(80)))
        batch.add_column(
            sa.Column(
                "revision_lineage_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    op.create_index(
        "uq_workflow_published_draft",
        "workflow_versions",
        [
            "workflow_id",
            "published_from_draft_revision",
            "published_from_draft_digest",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_workflow_published_draft", table_name="workflow_versions")
    with op.batch_alter_table("workflow_versions") as batch:
        batch.drop_column("revision_lineage_json")
        batch.drop_column("published_from_draft_digest")
        batch.drop_column("published_from_draft_revision")
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("draft_digest")
    with op.batch_alter_table("replay_evidence") as batch:
        batch.drop_column("human_patch_id")
        batch.drop_column("proposal_id")
        batch.drop_column("patched_definition_digest")
        batch.drop_column("patch_digest")
        batch.drop_column("base_definition_digest")
    op.drop_index("uq_human_patch_content", table_name="human_patches")
    op.drop_table("human_patches")
    op.drop_index("uq_revision_proposal_content", table_name="revision_proposals")
    op.drop_table("revision_proposals")
