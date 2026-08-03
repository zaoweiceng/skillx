import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.mcp import (
    MCPContractDriftError,
    call_snapshot_through_contract,
    discover_tools,
    register_server,
    scan_dependent_workflows,
)
from app.infrastructure.database import (
    MCPToolSnapshot,
    SessionLocal,
    Workflow,
    WorkflowVersion,
)


class MCPHttpHandler(BaseHTTPRequestHandler):
    # 通过类属性控制行为，便于单个测试切换 SSE / 错误 / 超时 / 契约变体。
    use_sse = False
    tool_error = False
    slow = False
    variant = "v1"
    # 会话过期模拟：首次 tools/call 返回 404，要求客户端重新握手后重试。
    expire_session_once = False
    _expired = False
    _sessions_issued = 0

    def _send(self, payload: dict[str, Any], *, session_id: str | None = None) -> None:
        if self.use_sse:
            body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
            content_type = "text/event-stream"
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or "{}")
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized" or request_id is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            type(self)._sessions_issued += 1
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": self.headers.get("Mcp-Protocol-Version", ""),
                        "capabilities": {},
                        "serverInfo": {"name": "loopback", "version": "1"},
                    },
                },
                session_id=f"sess-{type(self)._sessions_issued}",
            )
            return
        if method == "tools/list":
            input_schema = {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            }
            if self.variant == "v2":
                input_schema["properties"]["unit"] = {"type": "string"}
                input_schema["required"] = ["city", "unit"]
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [
                            {
                                "name": "weather",
                                "description": "查询天气",
                                "inputSchema": input_schema,
                                "outputSchema": {
                                    "type": "object",
                                    "properties": {"temperature": {"type": "number"}},
                                    "required": ["temperature"],
                                },
                            }
                        ]
                    },
                }
            )
            return
        if method == "tools/call":
            # 会话过期一次：返回 404，客户端应重新握手后重试。
            if self.expire_session_once and not type(self)._expired:
                type(self)._expired = True
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.slow:
                time.sleep(2)
            if self.tool_error:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32000, "message": "tool exploded"},
                    }
                )
                return
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"structuredContent": {"temperature": 21.5}},
                }
            )
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            }
        )

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def http_mcp() -> Iterator[type[MCPHttpHandler]]:
    MCPHttpHandler.use_sse = False
    MCPHttpHandler.tool_error = False
    MCPHttpHandler.slow = False
    MCPHttpHandler.variant = "v1"
    MCPHttpHandler.expire_session_once = False
    MCPHttpHandler._expired = False
    MCPHttpHandler._sessions_issued = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), MCPHttpHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        MCPHttpHandler.port = server.server_port
        yield MCPHttpHandler
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _url(handler: type[MCPHttpHandler]) -> str:
    return f"http://127.0.0.1:{handler.port}/mcp"


async def _register(handler: type[MCPHttpHandler], timeout_seconds: int = 5) -> str:
    async with SessionLocal() as session, session.begin():
        _, version = await register_server(
            session,
            f"http-weather-{uuid4().hex[:8]}",
            "http",
            {"url": _url(handler), "timeoutSeconds": timeout_seconds},
        )
        return version.id


async def test_http_discover_and_call_json(http_mcp: type[MCPHttpHandler]) -> None:
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        assert len(snapshots) == 1
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {"city": "成都"})
    assert outcome.status == "succeeded"
    assert outcome.output == {"temperature": 21.5}


async def test_http_discover_and_call_sse(http_mcp: type[MCPHttpHandler]) -> None:
    http_mcp.use_sse = True
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {"city": "成都"})
    assert outcome.status == "succeeded"
    assert outcome.output == {"temperature": 21.5}


async def test_http_tool_error_maps_to_dependency_unavailable(
    http_mcp: type[MCPHttpHandler],
) -> None:
    http_mcp.tool_error = True
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {"city": "成都"})
    assert outcome.status == "failed"
    assert outcome.error_code == "DEPENDENCY_UNAVAILABLE"
    assert outcome.retryable is True


async def test_http_timeout_maps_to_dependency_unavailable(
    http_mcp: type[MCPHttpHandler],
) -> None:
    http_mcp.slow = True
    version_id = await _register(http_mcp, timeout_seconds=1)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {"city": "成都"})
    assert outcome.status == "failed"
    assert outcome.error_code == "DEPENDENCY_UNAVAILABLE"


async def test_http_input_validation_rejects_before_call(
    http_mcp: type[MCPHttpHandler],
) -> None:
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {})
    assert outcome.status == "failed"
    assert outcome.error_code == "INVALID_INPUT"


async def test_http_schema_change_rejects_silent_overwrite(
    http_mcp: type[MCPHttpHandler],
) -> None:
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        first = await discover_tools(session, version_id)
        original_digest = first[0].digest
    # 远端在同一 ServerVersion 下升级了 Tool 输入 Schema。
    http_mcp.variant = "v2"
    async with SessionLocal() as session, session.begin():
        with pytest.raises(MCPContractDriftError):
            await discover_tools(session, version_id)
    # 快照未被静默覆盖。
    async with SessionLocal() as session:
        snapshot = await session.scalar(
            select(MCPToolSnapshot).where(MCPToolSnapshot.server_version_id == version_id)
        )
        assert snapshot is not None and snapshot.digest == original_digest


async def test_http_tool_removed_is_contract_drift(http_mcp: type[MCPHttpHandler]) -> None:
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        await discover_tools(session, version_id)
    # 模拟远端改名/消失：把已存在快照改名，使其不在下次 discover 结果集中。
    async with SessionLocal() as session, session.begin():
        snapshot = await session.scalar(
            select(MCPToolSnapshot).where(MCPToolSnapshot.server_version_id == version_id)
        )
        assert snapshot is not None
        snapshot.name = "ghost-tool"
    async with SessionLocal() as session, session.begin():
        with pytest.raises(MCPContractDriftError):
            await discover_tools(session, version_id)


async def test_scan_dependent_workflows_flags_breaking_change(
    http_mcp: type[MCPHttpHandler],
) -> None:
    server_name = f"http-weather-{uuid4().hex[:8]}"
    # 第 1 版：discover v1 契约。
    async with SessionLocal() as session, session.begin():
        _, v1 = await register_server(
            session, server_name, "http", {"url": _url(http_mcp), "timeoutSeconds": 5}
        )
        v1_id = v1.id
        server_id = v1.server_id
    async with SessionLocal() as session, session.begin():
        old = (await discover_tools(session, v1_id))[0]
        old_snapshot_id, old_digest, tool_name = old.id, old.digest, old.name
    # 发布一个锁定 v1 快照的 WorkflowVersion。
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(id=str(uuid4()), name="wf", draft_revision=1, draft_json={})
        session.add(workflow)
        await session.flush()
        session.add(
            WorkflowVersion(
                id=str(uuid4()),
                workflow_id=workflow.id,
                version=1,
                definition_json={},
                digest="sha256:x",
                validation_json={
                    "mcpLocks": [
                        {
                            "nodeId": "weather",
                            "mcpToolSnapshotId": old_snapshot_id,
                            "digest": old_digest,
                            "toolName": tool_name,
                        }
                    ]
                },
            )
        )
    # 第 2 版：同一 Server 注册新 ServerVersion，远端 Tool 输入 Schema 变化。
    http_mcp.variant = "v2"
    async with SessionLocal() as session, session.begin():
        _, v2 = await register_server(
            session, server_name, "http", {"url": _url(http_mcp), "timeoutSeconds": 5}
        )
        v2_id = v2.id
    async with SessionLocal() as session, session.begin():
        await discover_tools(session, v2_id)
    async with SessionLocal() as session:
        results = await scan_dependent_workflows(session, server_id, v2_id)
    assert len(results) == 1
    assert results[0]["hasBreaking"] is True
    finding = results[0]["findings"][0]
    assert finding["toolName"] == tool_name
    assert finding["status"] == "breaking"
    assert finding["reason"] == "contract_changed"


async def test_http_reconnects_after_session_expiry(
    http_mcp: type[MCPHttpHandler],
) -> None:
    """会话过期（服务端 404）时，客户端重新握手一次并成功完成调用。"""

    http_mcp.expire_session_once = True
    version_id = await _register(http_mcp)
    async with SessionLocal() as session, session.begin():
        snapshots = await discover_tools(session, version_id)
        snapshot_id = snapshots[0].id
    async with SessionLocal() as session:
        outcome = await call_snapshot_through_contract(session, snapshot_id, {"city": "成都"})
    assert outcome.status == "succeeded"
    assert outcome.output == {"temperature": 21.5}
    # 触发过一次会话过期，说明重连路径被执行。
    assert MCPHttpHandler._expired is True

