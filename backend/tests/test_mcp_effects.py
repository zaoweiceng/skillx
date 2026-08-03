import sys
import textwrap
from pathlib import Path

import pytest

from app.application.compiler import WorkflowCompiler
from app.application.mcp import (
    MCPConfigurationError,
    classify_snapshot,
    discover_tools,
    register_server,
    validate_mcp_nodes,
)
from app.infrastructure.database import SessionLocal

# 无 annotations 的写 Tool：discovery 必须默认 effect=unknown、idempotent=false。
UNCLASSIFIED_SERVER = """
import json
import sys

TOOL = {
    "name": "notes.create",
    "description": "创建一条笔记（写操作，无 annotations）",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}


def respond(request_id, result):
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\\n"
    )
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            respond(request["id"], {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}})
        elif method == "tools/list":
            respond(request["id"], {"tools": [TOOL]})
        elif method == "tools/call":
            respond(
                request["id"],
                {"structuredContent": {"id": "note-1"}, "isError": False},
            )


if __name__ == "__main__":
    main()
"""


def _server_path(tmp_path: Path) -> Path:
    path = tmp_path / "notes_mcp.py"
    path.write_text(textwrap.dedent(UNCLASSIFIED_SERVER), encoding="utf-8")
    return path


def _mcp_workflow(
    snapshot_id: str,
    *,
    side_effect: bool,
    max_attempts: int = 1,
    node_idempotent: bool = True,
) -> dict:
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "notes"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "note",
                    "type": "mcp",
                    "displayName": "创建笔记",
                    "input": {"text": "${input.text}"},
                    "config": {"mcpToolSnapshotId": snapshot_id},
                    "sideEffect": side_effect,
                    "idempotent": node_idempotent,
                    "retry": {"maxAttempts": max_attempts},
                },
                {
                    "id": "output",
                    "type": "output",
                    "displayName": "输出",
                    "input": {"id": "${note.output.id}"},
                },
            ],
            "edges": [
                {"source": "input", "target": "note"},
                {"source": "note", "target": "output"},
            ],
        },
    }


async def _register_and_discover(tmp_path: Path):
    server_file = _server_path(tmp_path)
    async with SessionLocal() as session, session.begin():
        _, server_version = await register_server(
            session,
            "notes",
            "stdio",
            {"command": [sys.executable, str(server_file)], "timeoutSeconds": 5},
        )
        snapshots = await discover_tools(session, server_version.id)
        return snapshots[0].id


async def test_unclassified_tool_defaults_to_unknown_and_not_idempotent(tmp_path: Path) -> None:
    snapshot_id = await _register_and_discover(tmp_path)
    async with SessionLocal() as session:
        from app.infrastructure.database import MCPToolSnapshot

        snapshot = await session.get(MCPToolSnapshot, snapshot_id)
        assert snapshot is not None
        assert snapshot.side_effect == "unknown"
        assert snapshot.idempotent is False
        assert snapshot.effect_source == "unknown"
        assert snapshot.effect_review_status == "unreviewed"


async def test_publish_blocked_until_effect_reviewed(tmp_path: Path) -> None:
    snapshot_id = await _register_and_discover(tmp_path)
    definition = _mcp_workflow(snapshot_id, side_effect=True)
    compiled = WorkflowCompiler().compile(definition)
    # 未审核：validate_mcp_nodes 必须阻止发布。
    with pytest.raises(MCPConfigurationError) as exc_info:
        async with SessionLocal() as session:
            await validate_mcp_nodes(session, compiled.definition.spec.nodes)
    assert "未分类或未审核" in str(exc_info.value)

    # 人工审核为写操作、非幂等后可锁定。
    async with SessionLocal() as session, session.begin():
        await classify_snapshot(session, snapshot_id, effect="write", idempotent=False)
    async with SessionLocal() as session:
        locks = await validate_mcp_nodes(session, compiled.definition.spec.nodes)
    assert locks[0]["effect"] == "write"
    assert locks[0]["idempotent"] is False


async def test_non_idempotent_tool_rejects_auto_retry(tmp_path: Path) -> None:
    snapshot_id = await _register_and_discover(tmp_path)
    async with SessionLocal() as session, session.begin():
        await classify_snapshot(session, snapshot_id, effect="write", idempotent=False)
    # 节点声明幂等以绕过编译期检查，暴露 Tool 快照非幂等与自动重试的冲突。
    definition = _mcp_workflow(snapshot_id, side_effect=True, max_attempts=3, node_idempotent=True)
    compiled = WorkflowCompiler().compile(definition)
    with pytest.raises(MCPConfigurationError) as exc_info:
        async with SessionLocal() as session:
            await validate_mcp_nodes(session, compiled.definition.spec.nodes)
    assert "不得自动重试" in str(exc_info.value)


async def test_write_tool_requires_side_effect_declaration(tmp_path: Path) -> None:
    snapshot_id = await _register_and_discover(tmp_path)
    async with SessionLocal() as session, session.begin():
        await classify_snapshot(session, snapshot_id, effect="write", idempotent=False)
    definition = _mcp_workflow(snapshot_id, side_effect=False)
    compiled = WorkflowCompiler().compile(definition)
    with pytest.raises(MCPConfigurationError) as exc_info:
        async with SessionLocal() as session:
            await validate_mcp_nodes(session, compiled.definition.spec.nodes)
    assert "未声明" in str(exc_info.value)


async def test_classify_changes_digest(tmp_path: Path) -> None:
    snapshot_id = await _register_and_discover(tmp_path)
    async with SessionLocal() as session:
        from app.infrastructure.database import MCPToolSnapshot

        before = (await session.get(MCPToolSnapshot, snapshot_id)).digest
    async with SessionLocal() as session, session.begin():
        updated = await classify_snapshot(session, snapshot_id, effect="write", idempotent=False)
        after = updated.digest
    assert before != after
