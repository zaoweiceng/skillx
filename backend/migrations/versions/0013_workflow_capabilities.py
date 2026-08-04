"""Add workflow capability manifests, fragments and frozen plan snapshots.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("failure_observations") as batch:
        batch.add_column(
            sa.Column(
                "baseline_reproduced",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
    op.create_table(
        "workflow_capability_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workflow_version_id",
            sa.String(36),
            sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_digest", sa.String(80), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "uq_workflow_capability_version",
        "workflow_capability_manifests",
        ["workflow_version_id"],
        unique=True,
    )
    op.create_index(
        "ix_workflow_capability_status",
        "workflow_capability_manifests",
        ["status"],
    )
    op.create_index(
        "ix_workflow_capability_manifests_digest",
        "workflow_capability_manifests",
        ["digest"],
    )
    op.create_table(
        "workflow_fragment_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workflow_version_id",
            sa.String(36),
            sa.ForeignKey("workflow_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fragment_ref", sa.String(240), nullable=False),
        sa.Column("source_workflow_digest", sa.String(80), nullable=False),
        sa.Column("closure_digest", sa.String(80), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "uq_workflow_fragment_ref",
        "workflow_fragment_manifests",
        ["fragment_ref"],
        unique=True,
    )
    op.create_index(
        "ix_workflow_fragment_version",
        "workflow_fragment_manifests",
        ["workflow_version_id"],
    )
    op.create_index(
        "ix_workflow_fragment_manifests_digest",
        "workflow_fragment_manifests",
        ["digest"],
    )
    op.create_table(
        "plan_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=False),
        sa.Column("locks_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_conversation_run_id",
            sa.String(36),
            sa.ForeignKey("conversation_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("plan_signature", sa.String(80), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_plan_snapshots_digest", "plan_snapshots", ["digest"])
    op.create_index("ix_plan_signature", "plan_snapshots", ["plan_signature"])
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("workflow_version_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("plan_snapshot_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_runs_plan_snapshot_id",
            "plan_snapshots",
            ["plan_snapshot_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_run_execution_source",
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
        )
    op.create_table(
        "recovery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "checkpoint_id",
            sa.String(36),
            sa.ForeignKey("execution_checkpoints.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("plan_patch_json", sa.JSON(), nullable=True),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "uq_recovery_attempt",
        "recovery_attempts",
        ["checkpoint_id", "attempt"],
        unique=True,
    )
    op.create_table(
        "replay_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "observation_id",
            sa.String(36),
            sa.ForeignKey("failure_observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("baseline_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("patched_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_replay_evidence_observation_id", "replay_evidence", ["observation_id"])


def downgrade() -> None:
    op.drop_table("replay_evidence")
    op.drop_table("recovery_attempts")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("ck_run_execution_source", type_="check")
        batch.drop_constraint("fk_runs_plan_snapshot_id", type_="foreignkey")
        batch.drop_column("plan_snapshot_id")
        batch.alter_column("workflow_version_id", existing_type=sa.String(36), nullable=False)
    op.drop_table("plan_snapshots")
    op.drop_table("workflow_fragment_manifests")
    op.drop_table("workflow_capability_manifests")
    with op.batch_alter_table("failure_observations") as batch:
        batch.drop_column("baseline_reproduced")
