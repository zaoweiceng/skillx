"""Deduplicate maintenance by fault/version rather than by whole task.

Revision ID: 0004_learning_faults
Revises: 0003_tasks
"""

from alembic import op

revision = "0004_learning_faults"
down_revision = "0003_tasks"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "learning_jobs", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}
    ) as batch:
        batch.drop_constraint("uq_learning_jobs_task_id", type_="unique")


def downgrade():
    with op.batch_alter_table("learning_jobs") as batch:
        batch.create_unique_constraint("uq_learning_jobs_task_id", ["task_id", "kind"])
