"""Persist Browser Worker startup preflight facts.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-10
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_runtime_heartbeats",
        sa.Column("worker_id", sa.String(120), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chromium_path", sa.Text()),
        sa.Column("chromium_version", sa.String(120)),
        sa.Column("playwright_version", sa.String(80)),
        sa.Column(
            "launch_succeeded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_browser_runtime_heartbeats_checked_at",
        "browser_runtime_heartbeats",
        ["checked_at"],
    )

    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text("SELECT id, runtime_snapshot_json FROM runs")
        ).mappings()
    )
    for row in rows:
        raw = row["runtime_snapshot_json"]
        snapshot = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if "runtimeUsed" not in snapshot or "runtimeSelection" in snapshot:
            continue
        snapshot["runtimeSelection"] = snapshot.pop("runtimeUsed")
        connection.execute(
            sa.text(
                "UPDATE runs SET runtime_snapshot_json = :snapshot WHERE id = :run_id"
            ),
            {
                "run_id": row["id"],
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text("SELECT id, runtime_snapshot_json FROM runs")
        ).mappings()
    )
    for row in rows:
        raw = row["runtime_snapshot_json"]
        snapshot = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        if "runtimeSelection" not in snapshot or "runtimeUsed" in snapshot:
            continue
        snapshot["runtimeUsed"] = snapshot.pop("runtimeSelection")
        connection.execute(
            sa.text(
                "UPDATE runs SET runtime_snapshot_json = :snapshot WHERE id = :run_id"
            ),
            {
                "run_id": row["id"],
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
            },
        )
    op.drop_index(
        "ix_browser_runtime_heartbeats_checked_at",
        table_name="browser_runtime_heartbeats",
    )
    op.drop_table("browser_runtime_heartbeats")
