"""Persist pending selection, task checkpoints, replay fixtures and broker fencing.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("conversation_runs") as batch:
        batch.add_column(
            sa.Column(
                "claim_generation",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("claim_token", sa.String(80), nullable=True))
    with op.batch_alter_table("workflow_disclosure_calls") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default=sa.text("'pending'"),
            )
        )
        batch.add_column(sa.Column("error_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("replay_evidence") as batch:
        batch.add_column(sa.Column("fixture_digest", sa.String(80), nullable=True))
        batch.add_column(sa.Column("base_version_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column("patch_of_observation_id", sa.String(36), nullable=True)
        )

    op.create_table(
        "pending_selections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "disclosure_session_id",
            sa.String(36),
            sa.ForeignKey("workflow_disclosure_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_task", sa.Text(), nullable=False),
        sa.Column("selection_json", sa.JSON(), nullable=False),
        sa.Column("bindings_json", sa.JSON(), nullable=False),
        sa.Column("missing_inputs_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_pending_selections_conversation_id",
        "pending_selections",
        ["conversation_id"],
    )
    op.create_index(
        "ix_pending_selections_disclosure_session_id",
        "pending_selections",
        ["disclosure_session_id"],
    )
    op.create_index(
        "ix_pending_selections_status", "pending_selections", ["status"]
    )
    op.create_index(
        "ix_pending_selections_expires_at", "pending_selections", ["expires_at"]
    )

    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_kind", sa.String(24), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("source_digest", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("digest", sa.String(80), nullable=False),
        sa.Column("lease_owner", sa.String(120), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claim_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("claim_token", sa.String(80), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_task_checkpoint_source",
        "task_checkpoints",
        ["source_kind", "source_id"],
    )
    op.create_index("ix_task_checkpoints_status", "task_checkpoints", ["status"])

    op.create_table(
        "replay_fixtures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_run_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(36),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_digest", sa.String(80), nullable=False),
        sa.Column("input_digest", sa.String(80), nullable=False),
        sa.Column("output_digest", sa.String(80), nullable=False),
        sa.Column("capture_policy", sa.String(40), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_replay_fixtures_run_id", "replay_fixtures", ["run_id"])
    op.create_index(
        "uq_replay_fixture_node_run",
        "replay_fixtures",
        ["node_run_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("replay_fixtures")
    op.drop_table("task_checkpoints")
    op.drop_table("pending_selections")
    with op.batch_alter_table("replay_evidence") as batch:
        batch.drop_column("patch_of_observation_id")
        batch.drop_column("base_version_id")
        batch.drop_column("fixture_digest")
    with op.batch_alter_table("workflow_disclosure_calls") as batch:
        batch.drop_column("error_json")
        batch.drop_column("status")
    with op.batch_alter_table("conversation_runs") as batch:
        batch.drop_column("claim_token")
        batch.drop_column("claim_generation")
