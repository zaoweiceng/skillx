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


class MCPLockDriftError(MCPConfigurationError):
    """运行前发现 WorkflowVersion 锁定的 Tool 契约与当前快照不一致。

    已发布版本保存了 snapshot id + digest + effect + idempotent + timeout 的完整
    lock。运行时任何一项不一致（快照被删除、被原地改写、digest 变化等）都必须
    fail closed，不得调用 Tool——映射为 ``MCP_CONTRACT_DRIFT``。
    """


def classify_effect(annotations: dict[str, Any]) -> tuple[str, bool, str]:
    """从 MCP annotations 推断 effect/idempotent，仅作为**提示**。

    返回 ``(effect, idempotent, effect_source)``。annotations 缺失时默认
    ``effect="unknown"``、``idempotent=False``、来源 ``unknown``——未知 Tool 绝不被
    当作安全的幂等读操作。即便 annotations 明确声明只读/幂等，来源也只标记为
    ``annotation``，审核状态保持 ``unreviewed``，必须经本地 Registry 显式确认才能发布。
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
        # contract_digest 只覆盖远端 descriptor：discovery 据此侦测真实远端漂移，
        # 不受本地 effect/idempotent 审核影响（后者只改 digest，不改 contract_digest）。
        remote_contract = {
            "name": descriptor.name,
            "description": descriptor.description,
            "inputSchema": descriptor.input_schema,
            "outputSchema": descriptor.output_schema,
            "timeoutSeconds": timeout_seconds,
        }
        contract_digest = digest_json(remote_contract)
        # 完整契约 digest 纳入 effect/idempotent，作为发布 lock 的运行前核对键。
        full_contract = {**remote_contract, "effect": effect, "idempotent": idempotent}
        full_digest = digest_json(full_contract)
        existing = await session.scalar(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == version.id,
                MCPToolSnapshot.name == descriptor.name,
            )
        )
        if existing:
            # 只比较远端契约 digest：本地审核过的 Tool（digest 已随 effect 变化）
            # 不会被重复 discovery 误判为远端漂移。
            if existing.contract_digest != contract_digest:
                raise MCPContractDriftError(
                    f"MCP Tool {descriptor.name} 的远端契约 digest 已变化，"
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
            digest=full_digest,
            contract_digest=contract_digest,
            side_effect=effect,
            idempotent=idempotent,
            timeout_seconds=timeout_seconds,
            effect_source=effect_source,
            # annotation 仅作提示，审核状态一律保持 unreviewed，直到本地 Registry
            # 显式确认；未审核 Tool 不得发布写节点。
            effect_review_status="unreviewed",
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
    """人工审核 effect/idempotent 分类，并创建**新的完整**不可变 ServerVersion。

    绝不 UPDATE 已存在的 ToolSnapshot：已被 WorkflowVersion 锁定的旧快照必须保持
    原样。分类在该 Server 下新建一个 ServerVersion（分类修订），**克隆原 current
    版本的全部 Tool**，只替换目标 Tool 的审核 policy（effect/idempotent/review），
    其余 Tool 原样复制。ServerVersion digest 覆盖完整、稳定排序的 Tool 集合与 policy；
    只有完整版本构建成功后才把 server.current 切过去，避免 current 丢 Tool。
    """

    valid_effects = {"none", "write", "destructive", "unknown"}
    if effect not in valid_effects:
        raise MCPConfigurationError(f"effect 仅支持 {sorted(valid_effects)}")
    target = await session.get(MCPToolSnapshot, snapshot_id)
    if target is None:
        raise MCPConfigurationError("MCP ToolSnapshot 不存在")
    source_version = await session.get(MCPServerVersion, target.server_version_id)
    if source_version is None:
        raise MCPConfigurationError("MCP ServerVersion 不存在")
    # 克隆源版本的**全部** Tool，保证新 current 是完整快照集合。
    source_snapshots = (
        await session.scalars(
            select(MCPToolSnapshot).where(
                MCPToolSnapshot.server_version_id == source_version.id
            )
        )
    ).all()

    latest = await session.scalar(
        select(func.max(MCPServerVersion.version)).where(
            MCPServerVersion.server_id == source_version.server_id
        )
    )
    new_version = MCPServerVersion(
        id=str(uuid4()),
        server_id=source_version.server_id,
        version=(latest or 0) + 1,
        config_json=source_version.config_json,
        digest="",  # 完整 Tool 集合构建后回填。
        status="published",
    )
    session.add(new_version)
    await session.flush()

    cloned: list[MCPToolSnapshot] = []
    result_snapshot: MCPToolSnapshot | None = None
    for source in source_snapshots:
        is_target = source.id == target.id
        new_effect = effect if is_target else source.side_effect
        new_idempotent = idempotent if is_target else source.idempotent
        new_review = source.effect_review_status
        new_source = source.effect_source
        if is_target:
            new_review = "unreviewed" if effect == "unknown" else "reviewed"
            new_source = "registry"
        full_contract = {
            "name": source.name,
            "description": source.description,
            "inputSchema": source.input_schema_json,
            "outputSchema": source.output_schema_json,
            "effect": new_effect,
            "idempotent": new_idempotent,
            "timeoutSeconds": source.timeout_seconds,
        }
        clone = MCPToolSnapshot(
            id=str(uuid4()),
            server_version_id=new_version.id,
            name=source.name,
            description=source.description,
            input_schema_json=source.input_schema_json,
            output_schema_json=source.output_schema_json,
            digest=digest_json(full_contract),
            contract_digest=source.contract_digest,
            side_effect=new_effect,
            idempotent=new_idempotent,
            timeout_seconds=source.timeout_seconds,
            effect_source=new_source,
            effect_review_status=new_review,
        )
        session.add(clone)
        cloned.append(clone)
        if is_target:
            result_snapshot = clone
    await session.flush()

    # ServerVersion digest 覆盖完整、稳定排序（按 name）的 Tool 集合与其 policy。
    ordered = sorted(cloned, key=lambda item: item.name)
    new_version.digest = digest_json(
        {
            "server": source_version.server_id,
            "tools": [
                {
                    "name": item.name,
                    "digest": item.digest,
                    "effect": item.side_effect,
                    "idempotent": item.idempotent,
                    "review": item.effect_review_status,
                }
                for item in ordered
            ],
        }
    )
    # 只有完整版本构建成功后才切换 current。
    server = await session.get(MCPServer, source_version.server_id)
    if server is not None:
        server.current_version_id = new_version.id
    await session.flush()
    if result_snapshot is None:  # pragma: no cover - 目标必在源集合中。
        raise MCPConfigurationError("分类目标 Tool 不在源版本中")
    return result_snapshot


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
        # 发布时保存完整 lock：运行前逐项核对 id + digest + effect + idempotent + timeout，
        # 任何不一致 fail closed，保证已发布版本的执行语义不被事后分类/升级改变。
        locks.append(
            {
                "nodeId": node.id,
                "mcpToolSnapshotId": snapshot.id,
                "digest": snapshot.digest,
                "toolName": snapshot.name,
                "effect": snapshot.side_effect,
                "idempotent": snapshot.idempotent,
                "timeoutSeconds": snapshot.timeout_seconds,
                "effectSource": snapshot.effect_source,
            }
        )
    return locks


async def resolve_mcp_node(
    session: AsyncSession,
    node: Any,
    lock: dict[str, Any] | None = None,
) -> Any:
    """解析 MCP 节点为可执行配置，并在运行前逐项核对 WorkflowVersion 中的锁定契约。

    ``lock`` 来自发布时 ``validate_mcp_nodes`` 写入 WorkflowVersion 的 ``mcpLocks``。
    若提供 lock，则 snapshot id + digest + effect + idempotent + timeout 必须逐项一致，
    任何漂移抛出 :class:`MCPLockDriftError`，不调用 Tool。
    """

    if node.type.value != "mcp":
        return node
    snapshot_id = node.config.get("mcpToolSnapshotId")
    if lock is not None and lock.get("mcpToolSnapshotId") != snapshot_id:
        raise MCPLockDriftError(
            f"MCP 节点 {node.id} 锁定的 ToolSnapshot id 与运行配置不一致"
        )
    snapshot = await session.get(MCPToolSnapshot, snapshot_id)
    if snapshot is None:
        raise MCPLockDriftError(f"MCP 节点 {node.id} 锁定的 ToolSnapshot 已不存在")
    if lock is not None:
        expected = {
            "digest": lock.get("digest"),
            "effect": lock.get("effect"),
            "idempotent": lock.get("idempotent"),
            "timeoutSeconds": lock.get("timeoutSeconds"),
        }
        actual = {
            "digest": snapshot.digest,
            "effect": snapshot.side_effect,
            "idempotent": snapshot.idempotent,
            "timeoutSeconds": snapshot.timeout_seconds,
        }
        # timeoutSeconds 在早期 lock 可能缺失，缺失时不参与核对。
        if expected["timeoutSeconds"] is None:
            expected.pop("timeoutSeconds")
            actual.pop("timeoutSeconds")
        drift = {key: actual[key] for key in expected if expected[key] != actual[key]}
        if drift:
            raise MCPLockDriftError(
                f"MCP 节点 {node.id} 的 Tool {snapshot.name} 契约与发布锁定不一致: {drift}"
            )
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
