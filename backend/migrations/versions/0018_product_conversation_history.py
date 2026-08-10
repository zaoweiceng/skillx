"""Add product conversation history, turns, usage and presentations.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(
            sa.Column(
                "workflow_mode",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'enabled'"),
            )
        )
        batch.add_column(sa.Column("last_message_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE conversations SET last_message_at = "
        "(SELECT MAX(created_at) FROM messages WHERE messages.conversation_id = conversations.id)"
    )
    op.create_index(
        "ix_conversations_activity",
        "conversations",
        ["last_message_at", "id"],
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_message_id", sa.String(36)),
        sa.Column("assistant_message_id", sa.String(36)),
        sa.Column(
            "parent_turn_id",
            sa.String(36),
            sa.ForeignKey("conversation_turns.id", ondelete="SET NULL"),
        ),
        sa.Column("workflow_mode_snapshot", sa.String(16), nullable=False),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default=sa.text("'accepted'"),
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("elapsed_ms", sa.Integer()),
        *_timestamps(),
    )
    op.create_index(
        "ix_conversation_turns_conversation_id",
        "conversation_turns",
        ["conversation_id"],
    )
    op.create_index("ix_conversation_turns_status", "conversation_turns", ["status"])
    op.create_index(
        "ix_conversation_turns_conversation",
        "conversation_turns",
        ["conversation_id", "accepted_at", "id"],
    )

    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("turn_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_messages_turn_id",
            "conversation_turns",
            ["turn_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_messages_turn_id", "messages", ["turn_id"])

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("turn_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_runs_turn_id",
            "conversation_turns",
            ["turn_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_runs_turn_id", "runs", ["turn_id"])
    op.create_index(
        "ix_runs_component_created",
        "runs",
        ["component_id", "created_at", "id"],
    )
    op.create_index(
        "ix_runs_workflow_version_created",
        "runs",
        ["workflow_version_id", "created_at", "id"],
    )

    with op.batch_alter_table("conversation_runs") as batch:
        batch.add_column(sa.Column("turn_id", sa.String(36)))
        batch.create_foreign_key(
            "fk_conversation_runs_turn_id",
            "conversation_turns",
            ["turn_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_conversation_runs_turn_id", "conversation_runs", ["turn_id"])

    with op.batch_alter_table("workflow_versions") as batch:
        batch.add_column(
            sa.Column(
                "presentation_policy_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )

    op.create_table(
        "run_presentations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("producer", sa.String(20), nullable=False),
        sa.Column("model", sa.String(120)),
        sa.Column("config_digest", sa.String(80)),
        sa.Column("prompt_template_digest", sa.String(80)),
        sa.Column("error_json", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("elapsed_ms", sa.Integer()),
        *_timestamps(),
    )
    op.create_index(
        "ix_run_presentations_run_id", "run_presentations", ["run_id"]
    )
    op.create_index(
        "ix_run_presentations_status", "run_presentations", ["status"]
    )
    op.create_index(
        "uq_run_presentation_revision",
        "run_presentations",
        ["run_id", "revision"],
        unique=True,
    )

    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "turn_id",
            sa.String(36),
            sa.ForeignKey("conversation_turns.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "conversation_run_id",
            sa.String(36),
            sa.ForeignKey("conversation_runs.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "presentation_id",
            sa.String(36),
            sa.ForeignKey("run_presentations.id", ondelete="CASCADE"),
        ),
        sa.Column("call_id", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(80)),
        sa.Column("model", sa.String(120)),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("reasoning_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column(
            "usage_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_model_usage_call_attempt",
        "model_usage",
        ["call_id", "attempt"],
        unique=True,
    )
    op.create_index(
        "ix_model_usage_turn", "model_usage", ["turn_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("model_usage")
    op.drop_table("run_presentations")
    with op.batch_alter_table("workflow_versions") as batch:
        batch.drop_column("presentation_policy_json")
    op.drop_index("ix_conversation_runs_turn_id", table_name="conversation_runs")
    with op.batch_alter_table("conversation_runs") as batch:
        batch.drop_constraint("fk_conversation_runs_turn_id", type_="foreignkey")
        batch.drop_column("turn_id")
    op.drop_index("ix_runs_workflow_version_created", table_name="runs")
    op.drop_index("ix_runs_component_created", table_name="runs")
    op.drop_index("ix_runs_turn_id", table_name="runs")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_runs_turn_id", type_="foreignkey")
        batch.drop_column("turn_id")
    op.drop_index("ix_messages_turn_id", table_name="messages")
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("fk_messages_turn_id", type_="foreignkey")
        batch.drop_column("turn_id")
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_activity", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("last_message_at")
        batch.drop_column("workflow_mode")
