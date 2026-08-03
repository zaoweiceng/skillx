from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import digest_json
from app.application.secrets import (
    SecretResolutionError,
    contains_secret,
    redact_secret_text,
    resolve_secret_handles,
)
from app.config import get_settings
from app.infrastructure.database import (
    MCPServer,
    MCPServerVersion,
    MCPToolSnapshot,
    WorkflowVersion,
)
from app.runtime.adapters import (
    AdapterError,
    MCPGateway,
    StdioMCPGateway,
    StreamableHttpMCPGateway,
)


class MCPConfigurationError(ValueError):
    pass


class MCPContractDriftError(MCPConfigurationError):
    """同一 ServerVersion 下同名 Tool 的契约 digest 发生变化。

    远端服务在配置不变时升级了 Tool 的输入/输出 Schema，静默复用旧快照会破坏
    历史可复现性。必须注册新的 ServerVersion 而不是覆盖既有快照。
    """


def classify_effect(annotations: dict[str, Any]) -> tuple[str, bool, str]:
    """从 MCP annotations 推断 effect/idempotent，仅作为提示。

    返回 ``(effect, idempotent, effect_source)``。annotations 缺失时默认
    ``effect="unknown"``、``idempotent=False``、来源 ``unknown``——未知 Tool 绝不被
    当作安全的幂等读操作。只有 annotations 明确声明只读/幂等时才降级为宽松语义，
    且来源标记为 ``annotation`` 以便审核区分。
    """

    if not isinstance(annotations, dict) or not annotations:
        return "unknown", False, "unknown"
    read_only = annotations.get("readOnlyHint")
    destructive = annotations.get("destructiveHint")
    idempotent_hint = annotations.get("idempotentHint")
    if read_only is True:
        return "none", True, "annotation"
    if destructive is True:
        return "destructive", bool(idempotent_hint is True), "annotation"
    # 存在 annotations 但未显式声明只读：视为写操作，除非显式 idempotentHint。
    return "write", bool(idempotent_hint is True), "annotation"


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
    discovered_names = {descriptor.name for descriptor in descriptors}
    existing_snapshots = (
        await session.scalars(
            select(MCPToolSnapshot).where(MCPToolSnapshot.server_version_id == version.id)
        )
    ).all()
    # Tool 在同一 ServerVersion 下消失，同样破坏契约锁定，必须显式感知。
    disappeared = sorted(
        item.name for item in existing_snapshots if item.name not in discovered_names
    )
    if disappeared:
        raise MCPContractDriftError(
            "MCP Tool 在同一 ServerVersion 下消失: " + ", ".join(disappeared)
        )
    snapshots: list[MCPToolSnapshot] = []
    for descriptor in descriptors:
        effect, idempotent, effect_source = classify_effect(descriptor.annotations)
        timeout_seconds = version.config_json.get("timeoutSeconds", 30)
        # 契约 digest 纳入副作用/幂等/超时：这些语义变化必须产生新快照或新
        # ServerVersion，不能静默沿用旧的乐观默认。
        contract = {
            "name": descriptor.name,
            "description": descriptor.description,
            "inputSchema": descriptor.input_schema,
            "outputSchema": descriptor.output_schema,
            "effect": effect,
            "idempotent": idempotent,
            "timeoutSeconds": timeout_seconds,
        }
        contract_digest = digest_json(contract)
        existing = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version.id,
                MCPToolSnapshot.name == descriptor.name,
            )
        )
        if existing:
            if existing.digest != contract_digest:
                raise MCPContractDriftError(
                    f"MCP Tool {descriptor.name} 的契约 digest 已变化，"
                    "请注册新的 ServerVersion 而非静默覆盖"
                )
            snapshots.append(existing)
            continue
        snapshot = MCPToolSnapshot(
            id=str(uuid4()),
            server_version_id=version.id,
            name=descriptor.name,
            description=descriptor.description,
            input_schema_json=descriptor.input_schema,
            output_schema_json=descriptor.output_schema,
            digest=contract_digest,
            side_effect=effect,
            idempotent=idempotent,
            timeout_seconds=timeout_seconds,
            effect_source=effect_source,
            # annotation 来源视为已审核提示；unknown 必须人工确认后方可发布写节点。
            effect_review_status="reviewed" if effect_source == "annotation" else "unreviewed",
        )
        session.add(snapshot)
        snapshots.append(snapshot)
    await session.flush()
    return snapshots


async def scan_dependent_workflows(
    session: AsyncSession,
    server_id: str,
    new_server_version_id: str,
) -> list[dict[str, Any]]:
    """扫描依赖某 MCP Server 早期 ToolSnapshot 的 WorkflowVersion。

    对每个受影响的 WorkflowVersion，比较其锁定的 Tool 与新 ServerVersion 中同名
    Tool 的契约 digest：digest 一致标记为 compatible，变化或消失标记为 breaking。
    仅做只读分析，不修改任何已发布版本。
    """

    new_snapshots = (
        await session.scalars(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == new_server_version_id
            )
        )
    ).all()
    new_by_name = {item.name: item for item in new_snapshots}
    # 该 Server 所有 ServerVersion 下的快照 id → (toolName, digest)。
    server_versions = (
        await session.scalars(
            select(MCPServerVersion.id).where(MCPServerVersion.server_id == server_id)
        )
    ).all()
    server_snapshots = (
        await session.scalars(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id.in_(server_versions)
            )
        )
    ).all()
    snapshot_by_id = {item.id: item for item in server_snapshots}

    results: list[dict[str, Any]] = []
    versions = (await session.scalars(select(WorkflowVersion))).all()
    for workflow_version in versions:
        locks = (workflow_version.validation_json or {}).get("mcpLocks") or []
        findings: list[dict[str, Any]] = []
        for lock in locks:
            locked = snapshot_by_id.get(lock.get("mcpToolSnapshotId"))
            if locked is None:
                continue
            if locked.server_version_id == new_server_version_id:
                continue  # 已锁定到新版本本身，无需比较。
            candidate = new_by_name.get(locked.name)
            if candidate is None:
                findings.append(
                    {"toolName": locked.name, "status": "breaking", "reason": "tool_removed"}
                )
            elif candidate.digest != locked.digest:
                findings.append(
                    {
                        "toolName": locked.name,
                        "status": "breaking",
                        "reason": "contract_changed",
                        "newSnapshotId": candidate.id,
                    }
                )
            else:
                findings.append(
                    {
                        "toolName": locked.name,
                        "status": "compatible",
                        "newSnapshotId": candidate.id,
                    }
                )
        if findings:
            results.append(
                {
                    "workflowVersionId": workflow_version.id,
                    "workflowId": workflow_version.workflow_id,
                    "findings": findings,
                    "hasBreaking": any(item["status"] == "breaking" for item in findings),
                }
            )
    return results


async def classify_snapshot(
    session: AsyncSession,
    snapshot_id: str,
    *,
    effect: str,
    idempotent: bool,
) -> MCPToolSnapshot:
    """人工审核并覆盖某 ToolSnapshot 的 effect/idempotent 分类。

    effect/idempotent 属于契约语义，变更后 digest 必须随之变化，从而使锁定旧 digest 的
    历史版本显式感知漂移。审核后来源标记为 ``registry``、状态为 ``reviewed``。
    """

    valid_effects = {"none", "write", "destructive", "unknown"}
    if effect not in valid_effects:
        raise MCPConfigurationError(f"effect 仅支持 {sorted(valid_effects)}")
    snapshot = await session.get(MCPToolSnapshot, snapshot_id)
    if snapshot is None:
        raise MCPConfigurationError("MCP ToolSnapshot 不存在")
    contract = {
        "name": snapshot.name,
        "description": snapshot.description,
        "inputSchema": snapshot.input_schema_json,
        "outputSchema": snapshot.output_schema_json,
        "effect": effect,
        "idempotent": idempotent,
        "timeoutSeconds": snapshot.timeout_seconds,
    }
    snapshot.side_effect = effect
    snapshot.idempotent = idempotent
    snapshot.effect_source = "registry"
    snapshot.effect_review_status = "unreviewed" if effect == "unknown" else "reviewed"
    snapshot.digest = digest_json(contract)
    await session.flush()
    return snapshot


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
        # 未分类 Tool 不得按乐观默认发布：必须经 Registry 审核确定 effect/idempotent。
        if snapshot.side_effect == "unknown" or snapshot.effect_review_status == "unreviewed":
            raise MCPConfigurationError(
                f"MCP 节点 {node.id} 的 Tool {snapshot.name} 副作用未分类或未审核，"
                "发布前必须显式确认 effect 与 idempotent"
            )
        # 写/破坏性 Tool 必须由 Workflow 节点显式声明 sideEffect。
        if snapshot.side_effect != "none" and not node.side_effect:
            raise MCPConfigurationError(
                f"MCP 节点 {node.id} 未声明 Tool 的 {snapshot.side_effect} 副作用"
            )
        # 非幂等 Tool 的 maxAttempts 必须为 1，禁止自动重试。
        if not snapshot.idempotent and node.retry.max_attempts > 1:
            raise MCPConfigurationError(
                f"MCP 节点 {node.id} 的非幂等 Tool 不得自动重试（maxAttempts 必须为 1）"
            )
        locks.append(
            {
                "nodeId": node.id,
                "mcpToolSnapshotId": snapshot.id,
                "digest": snapshot.digest,
                "toolName": snapshot.name,
                "effect": snapshot.side_effect,
                "idempotent": snapshot.idempotent,
                "effectSource": snapshot.effect_source,
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


def normalize_mcp_output(raw: dict[str, Any]) -> dict[str, Any]:
    """把 MCP Tool 原始响应归一化为结构化对象。"""

    structured = raw.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in raw.get("content", []):
        if item.get("type") == "text":
            parsed = json.loads(item.get("text", "{}"))
            if isinstance(parsed, dict):
                return parsed
    if isinstance(raw, dict) and "content" not in raw:
        return raw
    raise ValueError("MCP Tool 未返回结构化对象")


@dataclass
class MCPCallOutcome:
    status: str  # "succeeded" | "failed"
    output: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    tool_name: str | None = None
    tool_digest: str | None = None


async def call_mcp_contract(
    transport: str,
    server_config: dict[str, Any],
    tool_name: str,
    tool_digest: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any] | None,
    arguments: dict[str, Any],
    secret_values: list[str],
) -> MCPCallOutcome:
    """MCP 单一调用路径：输入校验 → 调用 → 归一化 → 输出校验 → 脱敏 → 错误映射。

    Workflow Executor 与 `/mcp-tools/{id}/test` 共用此函数，保证“测试成功”与真实
    节点运行行为一致。
    """

    errors = list(Draft202012Validator(input_schema).iter_errors(arguments))
    if errors:
        return MCPCallOutcome(
            status="failed",
            error_code="INVALID_INPUT",
            error_message=errors[0].message,
            tool_name=tool_name,
            tool_digest=tool_digest,
        )
    try:
        async with open_gateway(transport, server_config) as gateway:
            raw = await gateway.call(tool_name, arguments)
        output = normalize_mcp_output(raw)
        if contains_secret(output, secret_values):
            raise ValueError("MCP Tool 输出包含 Secret，已阻止持久化")
        if output_schema:
            Draft202012Validator(output_schema).validate(output)
        return MCPCallOutcome(
            status="succeeded",
            output=output,
            tool_name=tool_name,
            tool_digest=tool_digest,
        )
    except (AdapterError, httpx.HTTPError, TimeoutError) as exc:
        # 传输层不可用（超时、连接失败、HTTP 状态错误、进程退出）映射为可重试依赖错误。
        return MCPCallOutcome(
            status="failed",
            error_code="DEPENDENCY_UNAVAILABLE",
            error_message=redact_secret_text(str(exc), secret_values),
            retryable=True,
            tool_name=tool_name,
            tool_digest=tool_digest,
        )
    except Exception as exc:
        message = redact_secret_text(str(exc), secret_values)
        return MCPCallOutcome(
            status="failed",
            error_code="CONTRACT_VIOLATION",
            error_message=f"MCP Tool 输出不符合契约: {message}",
            tool_name=tool_name,
            tool_digest=tool_digest,
        )


async def call_snapshot_through_contract(
    session: AsyncSession,
    snapshot_id: str,
    arguments: dict[str, Any],
) -> MCPCallOutcome:
    """从 ToolSnapshot 解析上下文并走统一契约调用路径（供测试接口使用）。"""

    snapshot = await session.get(MCPToolSnapshot, snapshot_id)
    if snapshot is None:
        raise MCPConfigurationError("MCP ToolSnapshot 不存在")
    version = await session.get(MCPServerVersion, snapshot.server_version_id)
    server = await session.get(MCPServer, version.server_id if version else "")
    if version is None or server is None:
        raise MCPConfigurationError("MCP ServerVersion 不存在")
    resolved_config, secret_values = await resolve_server_config(session, version.config_json)
    return await call_mcp_contract(
        transport=server.transport,
        server_config=resolved_config,
        tool_name=snapshot.name,
        tool_digest=snapshot.digest,
        input_schema=snapshot.input_schema_json,
        output_schema=snapshot.output_schema_json,
        arguments=arguments,
        secret_values=secret_values,
    )
