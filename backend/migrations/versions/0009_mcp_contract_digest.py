"""Split MCP tool remote-contract digest from local effect-review policy.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-04

005-B：MCPToolSnapshot 新增 ``contract_digest``——只覆盖远端 descriptor（name/
description/inputSchema/outputSchema/timeout），用于 discovery 侦测真实远端契约漂移。
原 ``digest`` 继续覆盖完整契约（含 effect/idempotent），作为发布 lock 的运行前核对键。
两者分离后，本地人工分类（只改 effect/idempotent）不会被再次 discovery 误判为远端漂移。
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _remote_contract_digest(row: sa.engine.Row) -> str:
    """用与运行时一致的 canonical 算法，从旧行字段重算 remote-only contract_digest。

    只覆盖远端 descriptor（name/description/inputSchema/outputSchema/timeout），不含本地
    effect/idempotent；与 discover_tools 的 remote_contract 结构一一对应，避免非空 0008
    数据升级后首次 discovery 因两套算法比较不同 hash 而误报 MCPContractDrift。
    """

    remote_contract = {
        "name": row.name,
        "description": row.description or "",
        "inputSchema": json.loads(row.input_schema_json)
        if isinstance(row.input_schema_json, str)
        else row.input_schema_json,
        "outputSchema": json.loads(row.output_schema_json)
        if isinstance(row.output_schema_json, str)
        else row.output_schema_json,
        "timeoutSeconds": row.timeout_seconds,
    }
    return "sha256:" + hashlib.sha256(_canonical(remote_contract).encode()).hexdigest()


def upgrade() -> None:
    op.add_column(
        "mcp_tool_snapshots",
        sa.Column("contract_digest", sa.String(length=80), nullable=False, server_default=""),
    )
    # 回填：用运行时 canonical 算法从旧行字段重算 remote-only contract_digest，
    # 而非直接把旧 digest（含 effect/idempotent）赋值——那会造成首次 discovery 伪漂移。
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, name, description, input_schema_json, output_schema_json, "
            "timeout_seconds FROM mcp_tool_snapshots"
        )
    ).fetchall()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE mcp_tool_snapshots SET contract_digest = :digest WHERE id = :id"
            ),
            {"digest": _remote_contract_digest(row), "id": row.id},
        )


def downgrade() -> None:
    op.drop_column("mcp_tool_snapshots", "contract_digest")
