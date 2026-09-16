import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _alembic(database: Path, revision: str) -> None:
    environment = {
        **os.environ,
        "SKILLX_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
            row[1] for row in connection.execute("PRAGMA table_info(conversation_runs)")
        }
        snapshot_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(mcp_tool_snapshots)")
        }
        observation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(failure_observations)")
        }
        disclosure_call_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_disclosure_calls)")
        }
        workflow_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflows)")}
        fixture_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(replay_fixtures)")
        }
        evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(replay_evidence)")
        }
        version_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_versions)")
        }
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        model_usage_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(model_usage)")
        }
        component_columns = {row[1] for row in connection.execute("PRAGMA table_info(components)")}
    assert revision == ("0002_traex_model",)
    assert {
        "runs",
        "capability_versions",
        "mcp_tool_snapshots",
        "distillations",
        "conversation_run_events",
        "execution_checkpoints",
        "output_substitutions",
        "failure_observations",
        "workflow_capability_manifests",
        "workflow_fragment_manifests",
        "plan_snapshots",
        "recovery_attempts",
        "replay_evidence",
        "pending_selections",
        "task_checkpoints",
        "replay_fixtures",
        "browser_runtime_heartbeats",
        "revision_proposals",
        "human_patches",
        "conversation_turns",
        "run_presentations",
        "model_usage",
    } <= tables
    assert {
        "attempted_at",
        "request_dispatched_at",
        "completed_at",
        "error_code",
        "protocol",
        "runtime_type",
        "adapter_version",
        "adapter_digest",
    } <= model_usage_columns
    assert {"seed_key", "managed_revision"} <= component_columns
    assert "run_type" in run_type_columns
    assert "error_json" in run_type_columns
    assert {
        "lease_owner",
        "lease_expires_at",
        "next_event_seq",
        "claim_generation",
        "claim_token",
    } <= run_type_columns
    assert {"effect_source", "effect_review_status", "contract_digest"} <= snapshot_columns
    assert {
        "baseline_reproduced",
        "evidence_kind",
        "reproduction_policy_json",
        "evidence_ids_json",
    } <= observation_columns
    assert {"status", "error_json"} <= disclosure_call_columns
    assert {
        "draft_base_version_id",
        "draft_metadata_json",
        "draft_digest",
    } <= workflow_columns
    assert "evidence_kind" in fixture_columns
    assert {
        "base_definition_digest",
        "patch_digest",
        "patched_definition_digest",
        "proposal_id",
        "human_patch_id",
    } <= evidence_columns
    assert {
        "published_from_draft_revision",
        "published_from_draft_digest",
        "revision_lineage_json",
        "presentation_policy_json",
    } <= version_columns
    assert {"workflow_mode", "last_message_at"} <= conversation_columns
    assert {"turn_id", "runtime_snapshot_json"} <= run_columns
    with sqlite3.connect(database) as connection:
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(model_usage)")}
    assert {"runtime_used", "runtime_digest", "fallback_reason"} <= usage_columns
    with sqlite3.connect(database) as connection:
        checkpoint_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(execution_checkpoints)")
        }
    assert {"lease_owner", "recovery_node_run_id", "approval_status"} <= checkpoint_columns


def test_schema_matches_current_models_and_upgrade_is_repeatable(tmp_path: Path) -> None:
    import sqlalchemy as sa
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.infrastructure.database import Base

    database = tmp_path / "schema.db"
    _alembic(database, "head")
    _alembic(database, "head")
    with sa.create_engine(f"sqlite:///{database}").connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_reset_replaces_legacy_and_interrupted_migration_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0017');
            CREATE TABLE conversations (id TEXT PRIMARY KEY);
            INSERT INTO conversations VALUES ('old');
            CREATE TABLE _alembic_tmp_conversations (id TEXT);
        """)
    result = subprocess.run(
        [sys.executable, "-m", "app.reset_database", "--confirm-reset"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "SKILLX_DATABASE_URL": f"sqlite+aiosqlite:///{database}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_traex_model",
        )
        assert connection.execute("SELECT count(*) FROM conversations").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'"
            ).fetchall()
            == []
        )


def test_failed_sqlite_ddl_rolls_back_schema_and_revision(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "atomic.db"
    _alembic(database, "head")
    scripts = tmp_path / "migrations"
    shutil.copytree(root / "migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "failure.py").write_text("""from alembic import op
import sqlalchemy as sa
revision = "injected_failure"
down_revision = "0002_traex_model"
def upgrade():
    op.create_table("_alembic_tmp_probe", sa.Column("id", sa.Integer()))
    raise RuntimeError("injected migration interruption")
""")
    config = tmp_path / "alembic.ini"
    config.write_text(
        (root / "alembic.ini")
        .read_text()
        .replace("script_location = migrations", f"script_location = {scripts}")
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(config), "upgrade", "head"],
        cwd=root,
        env={**os.environ, "SKILLX_DATABASE_URL": f"sqlite+aiosqlite:///{database}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "injected migration interruption" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0002_traex_model",
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='_alembic_tmp_probe'"
            ).fetchall()
            == []
        )
