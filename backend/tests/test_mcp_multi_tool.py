"""005-B：多 Tool MCP Server 分类修订不可变性测试。

分类其中一个 Tool 后：新 current 版本必须包含全部 Tool、旧版本所有行不变、
重复 discovery 稳定（不误判本地审核为远端漂移）、任一 Tool 的真实远端契约变化仍
fail closed。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.application.mcp import (
    MCPContractDriftError,
    classify_snapshot,
    discover_tools,
    register_server,
)
from app.infrastructure.database import (
    MCPServer,
    MCPServerVersion,
    MCPToolSnapshot,
    SessionLocal,
)

# 两 Tool STDIO Server。测试通过重写文件内容模拟远端契约变化（真实漂移）。
TWO_TOOL_SERVER = """
import json
import sys

CREATE_SCHEMA = __CREATE_SCHEMA__

TOOLS = [
    {
        "name": "notes.create",
        "description": "创建笔记（写操作）",
        "inputSchema": CREATE_SCHEMA,
        "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "notes.read",
        "description": "读取笔记（只读）",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
    },
]


def respond(request_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            respond(request["id"], {"protocolVersion": "2025-03-26", "capabilities": {}})
        elif method == "tools/list":
            respond(request["id"], {"tools": TOOLS})
        elif method == "tools/call":
            respond(request["id"], {"structuredContent": {"id": "n-1"}, "isError": False})


if __name__ == "__main__":
    main()
"""

_V1_SCHEMA = (
    '{"type": "object", "properties": {"text": {"type": "string"}}, '
    '"required": ["text"], "additionalProperties": False}'
)
_V2_SCHEMA = (
    '{"type": "object", "properties": {"text": {"type": "string"}, '
    '"title": {"type": "string"}}, "required": ["text", "title"], '
    '"additionalProperties": False}'
)


def _write_server(path: Path, schema: str) -> None:
    path.write_text(TWO_TOOL_SERVER.replace("__CREATE_SCHEMA__", schema), encoding="utf-8")


def _server_path(tmp_path: Path) -> Path:
    path = tmp_path / "two_tool_mcp.py"
    _write_server(path, _V1_SCHEMA)
    return path


async def _register_and_discover(tmp_path: Path) -> tuple[str, list[str]]:
    server_file = _server_path(tmp_path)
    async with SessionLocal() as session, session.begin():
        _, version = await register_server(
            session,
            "notes-multi",
            "stdio",
            {"command": [sys.executable, str(server_file)], "timeoutSeconds": 5},
        )
        snapshots = await discover_tools(session, version.id)
        return version.id, [item.id for item in snapshots]


async def test_classify_keeps_all_tools_in_new_current(tmp_path: Path) -> None:
    version_id, snapshot_ids = await _register_and_discover(tmp_path)
    assert len(snapshot_ids) == 2
    # 分类 notes.create 为 write/非幂等。
    async with SessionLocal() as session, session.begin():
        create = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version_id,
                MCPToolSnapshot.name == "notes.create",
            )
        )
        reviewed = await classify_snapshot(
            session, create.id, effect="write", idempotent=False
        )
        new_version_id = reviewed.server_version_id

    async with SessionLocal() as session:
        # 新 current 版本包含全部 Tool。
        server = await session.scalar(
            select(MCPServer).where(MCPServer.name == "notes-multi")
        )
        assert server.current_version_id == new_version_id
        new_tools = (
            await session.scalars(
                select(MCPToolSnapshot).where(
                    MCPToolSnapshot.server_version_id == new_version_id
                )
            )
        ).all()
        assert {item.name for item in new_tools} == {"notes.create", "notes.read"}
        # 目标 Tool 已审核为 write；另一 Tool 原样克隆（仍 unreviewed）。
        by_name = {item.name: item for item in new_tools}
        assert by_name["notes.create"].side_effect == "write"
        assert by_name["notes.create"].effect_review_status == "reviewed"
        assert by_name["notes.read"].effect_review_status == "unreviewed"
        # 版本 digest 覆盖完整 Tool 集合。
        new_version = await session.get(MCPServerVersion, new_version_id)
        assert new_version.digest.startswith("sha256:")

        # 旧版本所有行不变。
        old_tools = (
            await session.scalars(
                select(MCPToolSnapshot).where(
                    MCPToolSnapshot.server_version_id == version_id
                )
            )
        ).all()
        assert {item.name for item in old_tools} == {"notes.create", "notes.read"}
        old_by_name = {item.name: item for item in old_tools}
        # 旧版本目标 Tool 仍为 unknown/unreviewed（未被原地修改）。
        assert old_by_name["notes.create"].side_effect == "unknown"
        assert old_by_name["notes.create"].effect_review_status == "unreviewed"


async def test_rediscovery_after_classify_is_stable(tmp_path: Path) -> None:
    version_id, _ = await _register_and_discover(tmp_path)
    async with SessionLocal() as session, session.begin():
        create = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version_id,
                MCPToolSnapshot.name == "notes.create",
            )
        )
        await classify_snapshot(session, create.id, effect="write", idempotent=False)
    # 对旧版本再次 discovery：本地审核差异不得被误判为远端 contract drift。
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
    assert len(snapshots) == 2


async def test_real_remote_contract_change_still_fails_closed(tmp_path: Path) -> None:
    server_file = _server_path(tmp_path)
    async with SessionLocal() as session, session.begin():
        _, version = await register_server(
            session,
            "notes-multi",
            "stdio",
            {"command": [sys.executable, str(server_file)], "timeoutSeconds": 5},
        )
        version_id = version.id
        await discover_tools(session, version_id)
        create = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version_id,
                MCPToolSnapshot.name == "notes.create",
            )
        )
        await classify_snapshot(session, create.id, effect="write", idempotent=False)
    # 真实远端契约变化（notes.create 的 inputSchema 变了）：即便分类之后也必须 fail closed。
    _write_server(server_file, _V2_SCHEMA)
    with pytest.raises(MCPContractDriftError):
        async with SessionLocal() as session, session.begin():
            await discover_tools(session, version_id)
