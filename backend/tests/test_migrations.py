import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_alembic_builds_empty_database_to_head(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    environment = {
        **os.environ,
        "SKILLX_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        run_type_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(conversation_runs)")
        }
        snapshot_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(mcp_tool_snapshots)")
        }
    assert revision == ("0007",)
    assert {
        "runs",
        "capability_versions",
        "mcp_tool_snapshots",
        "distillations",
        "conversation_run_events",
    } <= tables
    assert "run_type" in run_type_columns
    assert "error_json" in run_type_columns
    assert {"effect_source", "effect_review_status"} <= snapshot_columns
