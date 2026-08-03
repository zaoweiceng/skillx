from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.application.secrets import SecretResolutionError, resolve_secret_handles
from app.config import get_settings
from app.infrastructure.database import (
    MCPServer,
    MCPServerVersion,
    MCPToolSnapshot,
)
from app.runtime.adapters import (
    MCPGateway,
    StdioMCPGateway,
    StreamableHttpMCPGateway,
)


class MCPConfigurationError(ValueError):
    pass


def validate_server_config(
    transport: str,
    config: dict[str, Any],
    *,
    resolved: bool = False,
) -> None:
    if transport == "stdio":
        command = config.get("command")
        if not isinstance(command, list) or not command:
            raise MCPConfigurationError("STDIO Server 必须配置 command")
        executable = Path(command[0]).name
        if executable not in get_settings().mcp_allowed_executables:
            raise MCPConfigurationError("MCP 可执行文件不在白名单")
        environment = config.get("environment", {})
        if not isinstance(environment, dict):
            raise MCPConfigurationError("STDIO environment 必须为对象")
        for variable, reference in environment.items():
            if resolved and isinstance(variable, str) and isinstance(reference, str):
                continue
            if (
                not isinstance(variable, str)
                or not isinstance(reference, dict)
                or set(reference) != {"secretRef"}
                or not isinstance(reference["secretRef"], str)
            ):
                raise MCPConfigurationError(
                    "STDIO environment 只接受 {变量名: {secretRef: 名称}}"
                )
    elif transport == "http":
        url = config.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in {"http", "https"}:
            raise MCPConfigurationError("HTTP Server URL 无效")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise MCPConfigurationError("Local Demo 只允许 loopback MCP HTTP Server")
    else:
        raise MCPConfigurationError("transport 仅支持 stdio 或 http")


async def resolve_server_config(
    session: AsyncSession,
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    environment = config.get("environment", {})
    references = {
        variable: item["secretRef"]
        for variable, item in environment.items()
    }
    try:
        secrets = await resolve_secret_handles(session, list(references.values()))
    except SecretResolutionError as exc:
        raise MCPConfigurationError(str(exc)) from exc
    resolved_environment = {
        variable: secrets[name] for variable, name in references.items()
    }
    return (
        {**config, "environment": resolved_environment},
        list(secrets.values()),
    )


@asynccontextmanager
async def open_gateway(
    transport: str, config: dict[str, Any]
) -> AsyncGenerator[MCPGateway]:
    validate_server_config(transport, config, resolved=True)
    if transport == "stdio":
        gateway = StdioMCPGateway(
            command=config["command"],
            allowed_executables=set(get_settings().mcp_allowed_executables),
            environment=config.get("environment"),
            timeout_seconds=config.get("timeoutSeconds", 30),
        )
        async with gateway:
            yield gateway
    else:
        yield StreamableHttpMCPGateway(
            url=config["url"],
            timeout_seconds=config.get("timeoutSeconds", 30),
        )


async def register_server(
    session: AsyncSession,
    name: str,
    transport: str,
    config: dict[str, Any],
) -> tuple[MCPServer, MCPServerVersion]:
    validate_server_config(transport, config)
    server = await session.scalar(select(MCPServer).where(MCPServer.name == name))
    if server is None:
        server = MCPServer(
            id=str(uuid4()),
            name=name,
            transport=transport,
        )
        session.add(server)
        await session.flush()
    elif server.transport != transport:
        raise MCPConfigurationError("同名 MCP Server 不得更改 transport")
    latest = await session.scalar(
        select(func.max(MCPServerVersion.version)).where(MCPServerVersion.server_id == server.id)
    )
    version = MCPServerVersion(
        id=str(uuid4()),
        server_id=server.id,
        version=(latest or 0) + 1,
        config_json=config,
        digest=digest_json({"transport": transport, "config": config}),
        status="published",
    )
    session.add(version)
    await session.flush()
    server.current_version_id = version.id
    return server, version


async def discover_tools(
    session: AsyncSession,
    server_version_id: str,
) -> list[MCPToolSnapshot]:
    version = await session.get(MCPServerVersion, server_version_id)
    if version is None or version.status != "published":
        raise MCPConfigurationError("MCP ServerVersion 不可用")
    server = await session.get(MCPServer, version.server_id)
    if server is None:
        raise MCPConfigurationError("MCP Server 不存在")
    resolved_config, _ = await resolve_server_config(session, version.config_json)
    async with open_gateway(server.transport, resolved_config) as gateway:
        descriptors = await gateway.discover()
    snapshots: list[MCPToolSnapshot] = []
    for descriptor in descriptors:
        contract = {
            "name": descriptor.name,
            "description": descriptor.description,
            "inputSchema": descriptor.input_schema,
            "outputSchema": descriptor.output_schema,
        }
        existing = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version.id,
                MCPToolSnapshot.name == descriptor.name,
            )
        )
        if existing:
            snapshots.append(existing)
            continue
        snapshot = MCPToolSnapshot(
            id=str(uuid4()),
            server_version_id=version.id,
            name=descriptor.name,
            description=descriptor.description,
            input_schema_json=descriptor.input_schema,
            output_schema_json=descriptor.output_schema,
            digest=digest_json(contract),
            side_effect="none",
            idempotent=True,
            timeout_seconds=version.config_json.get("timeoutSeconds", 30),
        )
        session.add(snapshot)
        snapshots.append(snapshot)
    await session.flush()
    return snapshots


async def validate_mcp_nodes(
    session: AsyncSession,
    nodes: list[Any],
) -> list[dict[str, Any]]:
    locks: list[dict[str, Any]] = []
    for node in nodes:
        if node.type.value != "mcp":
            continue
        snapshot_id = node.config.get("mcpToolSnapshotId")
        snapshot = await session.get(MCPToolSnapshot, snapshot_id)
        if snapshot is None:
            raise MCPConfigurationError(f"MCP 节点 {node.id} 未锁定有效 ToolSnapshot")
        if snapshot.side_effect != "none" and not node.side_effect:
            raise MCPConfigurationError(
                f"MCP 节点 {node.id} 未声明 Tool 的 {snapshot.side_effect} 副作用"
            )
        if not snapshot.idempotent and node.retry.max_attempts > 1:
            raise MCPConfigurationError(
                f"MCP 节点 {node.id} 的非幂等 Tool 不得自动重试"
            )
        locks.append(
            {
                "nodeId": node.id,
                "mcpToolSnapshotId": snapshot.id,
                "digest": snapshot.digest,
                "toolName": snapshot.name,
            }
        )
    return locks


async def resolve_mcp_node(
    session: AsyncSession,
    node: Any,
) -> Any:
    if node.type.value != "mcp":
        return node
    snapshot = await session.get(MCPToolSnapshot, node.config.get("mcpToolSnapshotId"))
    if snapshot is None:
        raise MCPConfigurationError("锁定的 MCP ToolSnapshot 不存在")
    version = await session.get(MCPServerVersion, snapshot.server_version_id)
    server = await session.get(MCPServer, version.server_id if version else "")
    if version is None or server is None or version.status != "published":
        raise MCPConfigurationError("锁定的 MCP ServerVersion 不可用")
    resolved_server_config, secret_values = await resolve_server_config(
        session,
        version.config_json,
    )
    config = {
        **node.config,
        "_resolvedMcp": {
            "transport": server.transport,
            "serverConfig": resolved_server_config,
            "toolName": snapshot.name,
            "inputSchema": snapshot.input_schema_json,
            "outputSchema": snapshot.output_schema_json,
            "timeoutSeconds": snapshot.timeout_seconds,
            "digest": snapshot.digest,
            "secretValues": secret_values,
        },
    }
    return node.model_copy(update={"config": config}, deep=True)


async def call_tool_snapshot(
    session: AsyncSession,
    snapshot_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    snapshot = await session.get(MCPToolSnapshot, snapshot_id)
    if snapshot is None:
        raise MCPConfigurationError("MCP ToolSnapshot 不存在")
    version = await session.get(MCPServerVersion, snapshot.server_version_id)
    server = await session.get(MCPServer, version.server_id if version else "")
    if version is None or server is None:
        raise MCPConfigurationError("MCP ServerVersion 不存在")
    resolved_config, _ = await resolve_server_config(session, version.config_json)
    async with open_gateway(server.transport, resolved_config) as gateway:
        return await gateway.call(snapshot.name, arguments)
