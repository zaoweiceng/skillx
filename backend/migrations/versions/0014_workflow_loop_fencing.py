"""Close recovery fencing and persist workflow disclosure sessions.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("execution_checkpoints") as batch:
        batch.add_column(
            sa.Column(
                "claim_generation",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(sa.Column("claim_token", sa.String(80), nullable=True))
    with op.batch_alter_table("node_runs") as batch:
        batch.add_column(sa.Column("claim_generation", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("claim_token", sa.String(80), nullable=True))
    with op.batch_alter_table("plan_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "source_map_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    with op.batch_alter_table("failure_observations") as batch:
        batch.alter_column(
            "workflow_version_id", existing_type=sa.String(36), nullable=True
        )
        batch.add_column(sa.Column("plan_snapshot_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("source_node_run_id", sa.String(36), nullable=True))
        batch.add_column(
            sa.Column(
                "locks_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(sa.Column("recovered_outcome_json", sa.JSON(), nullable=True))
        batch.create_check_constraint(
            "ck_failure_observation_source",
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
        )
    with op.batch_alter_table("artifacts") as batch:
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("conversation_run_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_artifacts_conversation_run_id",
            "conversation_runs",
            ["conversation_run_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint(
            "ck_artifact_source",
            "(run_id IS NOT NULL) != (conversation_run_id IS NOT NULL)",
        )
        batch.create_index(
            "ix_artifacts_conversation_run_id", ["conversation_run_id"]
        )
    op.create_table(
        "workflow_disclosure_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("conversation_id", sa.String(36), nullable=True),
        sa.Column("agent_run_id", sa.String(120), nullable=True),
        sa.Column("disclosed_json", sa.JSON(), nullable=False),
        sa.Column("call_budget_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_workflow_disclosure_sessions_conversation_id",
        "workflow_disclosure_sessions",
        ["conversation_id"],
    )
    op.create_index(
        "ix_workflow_disclosure_sessions_agent_run_id",
        "workflow_disclosure_sessions",
        ["agent_run_id"],
    )
    op.create_index(
        "ix_workflow_disclosure_sessions_expires_at",
        "workflow_disclosure_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_workflow_disclosure_sessions_status",
        "workflow_disclosure_sessions",
        ["status"],
    )
    op.create_table(
        "workflow_disclosure_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(36),
            sa.ForeignKey("workflow_disclosure_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("request_summary_json", sa.JSON(), nullable=False),
        sa.Column("response_digest", sa.String(80), nullable=False),
        sa.Column("disclosure_level", sa.String(8), nullable=True),
        *_timestamps(),
    )
    op.create_index(
        "ix_disclosure_call_session",
        "workflow_disclosure_calls",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("workflow_disclosure_calls")
    op.drop_table("workflow_disclosure_sessions")
    with op.batch_alter_table("artifacts") as batch:
        batch.drop_index("ix_artifacts_conversation_run_id")
        batch.drop_constraint("ck_artifact_source", type_="check")
        batch.drop_constraint(
            "fk_artifacts_conversation_run_id", type_="foreignkey"
        )
        batch.drop_column("conversation_run_id")
        batch.alter_column("run_id", existing_type=sa.String(36), nullable=False)
    with op.batch_alter_table("failure_observations") as batch:
        batch.drop_constraint("ck_failure_observation_source", type_="check")
        batch.drop_column("recovered_outcome_json")
        batch.drop_column("locks_json")
        batch.drop_column("source_node_run_id")
        batch.drop_column("plan_snapshot_id")
        batch.alter_column(
            "workflow_version_id", existing_type=sa.String(36), nullable=False
        )
    with op.batch_alter_table("plan_snapshots") as batch:
        batch.drop_column("source_map_json")
    with op.batch_alter_table("node_runs") as batch:
        batch.drop_column("claim_token")
        batch.drop_column("claim_generation")
    with op.batch_alter_table("execution_checkpoints") as batch:
        batch.drop_column("claim_token")
        batch.drop_column("claim_generation")
