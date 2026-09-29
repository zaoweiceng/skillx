"""Durable task control and human interactions."""

import sqlalchemy as sa
from alembic import op

revision = "0003_tasks"
down_revision = "0002_traex_model"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id"),
            nullable=True,
        ),
        sa.Column(
            "turn_id", sa.String(length=36), sa.ForeignKey("conversation_turns.id"), nullable=True
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("goal", sa.String(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("runtime_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decisions", sa.Integer(), nullable=False),
        sa.Column("active_ms", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("turn_id"),
    )
    op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_table(
        "invocations",
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("scope", sa.String(length=80), nullable=False),
        sa.Column("call_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("runtime_json", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "call_key"),
    )
    op.create_index("ix_invocations_status", "invocations", ["status"])
    op.create_index("ix_invocations_task_id", "invocations", ["task_id"])
    op.create_table(
        "human_interactions",
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("call_key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_digest", sa.String(length=80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("answer_json", sa.JSON(), nullable=True),
        sa.Column("answer_key", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "call_key"),
    )
    op.create_index("ix_human_interactions_task_id", "human_interactions", ["task_id"])
    op.create_table(
        "learning_jobs",
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fingerprint"),
        sa.UniqueConstraint("task_id", "kind"),
    )
    op.create_index("ix_learning_jobs_status", "learning_jobs", ["status"])
    op.create_index("ix_learning_jobs_task_id", "learning_jobs", ["task_id"])


def downgrade():
    op.drop_table("learning_jobs")
    op.drop_table("human_interactions")
    op.drop_table("invocations")
    op.drop_table("task_events")
    op.drop_table("tasks")
