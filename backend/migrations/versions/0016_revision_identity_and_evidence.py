"""Close revision identity, evidence policy and Agent artifact fencing.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(
            sa.Column("draft_base_version_id", sa.String(36), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "draft_metadata_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    with op.batch_alter_table("failure_observations") as batch:
        batch.add_column(
            sa.Column(
                "evidence_kind",
                sa.String(40),
                nullable=False,
                server_default=sa.text("'unsupported'"),
            )
        )
        batch.add_column(
            sa.Column(
                "reproduction_policy_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column(
                "evidence_ids_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
    with op.batch_alter_table("replay_fixtures") as batch:
        batch.add_column(
            sa.Column(
                "evidence_kind",
                sa.String(40),
                nullable=False,
                server_default=sa.text("'unsupported'"),
            )
        )
    op.create_index(
        "uq_conversation_artifact_name",
        "artifacts",
        ["conversation_run_id", "kind", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_artifact_name", table_name="artifacts")
    with op.batch_alter_table("replay_fixtures") as batch:
        batch.drop_column("evidence_kind")
    with op.batch_alter_table("failure_observations") as batch:
        batch.drop_column("evidence_ids_json")
        batch.drop_column("reproduction_policy_json")
        batch.drop_column("evidence_kind")
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("draft_metadata_json")
        batch.drop_column("draft_base_version_id")
