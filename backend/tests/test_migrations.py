import hashlib
import json
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
            row[1]
            for row in connection.execute("PRAGMA table_info(conversation_runs)")
        }
        snapshot_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(mcp_tool_snapshots)")
        }
        observation_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(failure_observations)")
        }
        disclosure_call_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(workflow_disclosure_calls)"
            )
        }
    assert revision == ("0015",)
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
    } <= tables
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
    assert "baseline_reproduced" in observation_columns
    assert {"status", "error_json"} <= disclosure_call_columns
    with sqlite3.connect(database) as connection:
        checkpoint_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(execution_checkpoints)")
        }
    assert {"lease_owner", "recovery_node_run_id", "approval_status"} <= checkpoint_columns


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_non_empty_0008_to_head_recomputes_remote_contract_digest(tmp_path: Path) -> None:
    """带 MCP 数据的 0008→head 升级：contract_digest 按运行时 canonical 算法从行字段
    重算（remote-only），首次 rediscovery 不会伪漂移。"""

    database = tmp_path / "data-migration.db"
    _alembic(database, "0008")
    # 在 0008 插入两个 Tool（旧 digest 含本地 effect/idempotent，与 remote 契约不同）。
    input_schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    output_schema = {"type": "object", "properties": {"temp": {"type": "number"}}}
    tools = [
        ("tool-a", "weather.get", "查询天气"),
        ("tool-b", "forecast.get", "查询预报"),
    ]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO mcp_servers (id, name, transport, created_at, updated_at) "
            "VALUES ('srv', 'weather', 'stdio', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO mcp_server_versions (id, server_id, version, config_json, digest, "
            "status, created_at, updated_at) VALUES "
            "('sv', 'srv', 1, '{}', 'sha256:sv', 'published', '2026-01-01', '2026-01-01')"
        )
        for tool_id, name, description in tools:
            old_digest = "sha256:" + hashlib.sha256((name + "old").encode()).hexdigest()
            connection.execute(
                "INSERT INTO mcp_tool_snapshots (id, server_version_id, name, description, "
                "input_schema_json, output_schema_json, digest, side_effect, idempotent, "
                "timeout_seconds, effect_source, effect_review_status, created_at, updated_at) "
                "VALUES (?, 'sv', ?, ?, ?, ?, ?, 'none', 1, 30, 'registry', 'reviewed', "
                "'2026-01-01', '2026-01-01')",
                (
                    tool_id,
                    name,
                    description,
                    json.dumps(input_schema),
                    json.dumps(output_schema),
                    old_digest,
                ),
            )
        connection.commit()

    # 升级到 head：0009 应重算 remote-only contract_digest。
    _alembic(database, "head")

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT name, description, timeout_seconds, contract_digest, digest "
            "FROM mcp_tool_snapshots ORDER BY name"
        ).fetchall()
    assert len(rows) == 2
    for name, description, timeout_seconds, contract_digest, digest in rows:
        # contract_digest 与运行时 discover_tools 的 remote_contract 结构一致。
        expected = "sha256:" + hashlib.sha256(
            _canonical(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": input_schema,
                    "outputSchema": output_schema,
                    "timeoutSeconds": timeout_seconds,
                }
            ).encode()
        ).hexdigest()
        assert contract_digest == expected
        # 且不等于旧的完整 digest（含 effect/idempotent），证明没有直接回填。
        assert contract_digest != digest
