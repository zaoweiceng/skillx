"""Resolve explicit immutable capability dependencies at registration."""

from sqlalchemy import select

from app.infrastructure.database import Capability, CapabilityVersion


async def lock_dependencies(session, manifest):
    locks = {}
    for alias, dependency in manifest["spec"].get("capabilities", {}).items():
        kind = dependency.get("kind")
        if kind == "skillx":
            ref = dependency.get("ref", "")
            package, _, interface = ref.partition("#")
            name, _, version = package.rpartition("@")
            name = name.rsplit("/", 1)[-1]
            target = await session.scalar(
                select(CapabilityVersion)
                .join(Capability)
                .where(
                    Capability.name == name,
                    CapabilityVersion.version == version,
                    CapabilityVersion.status == "published",
                )
            )
            if not target or interface not in target.manifest_json["interfaces"]:
                raise ValueError(f"{alias}: 依赖必须引用已发布的精确版本和接口")
            target_interface = target.manifest_json["interfaces"][interface]
            locks[alias] = {
                "kind": kind,
                "versionId": target.id,
                "interface": interface,
                "digest": target.digest,
                "interfaceKind": target_interface["kind"],
                "effect": "unknown" if target_interface.get("effects") else "none",
            }
        elif kind == "mcp-tool":
            from app.infrastructure.database import MCPServer, MCPServerVersion, MCPToolSnapshot

            selector = dependency.get("selector", {})
            snapshot = await session.scalar(
                select(MCPToolSnapshot)
                .join(MCPServerVersion)
                .join(MCPServer)
                .where(
                    MCPServer.name == selector.get("server"),
                    MCPToolSnapshot.name == selector.get("toolName"),
                    MCPServerVersion.id == MCPServer.current_version_id,
                )
            )
            if snapshot is None or snapshot.effect_review_status != "reviewed":
                raise ValueError(f"{alias}: MCP 依赖不存在或尚未审核")
            locks[alias] = {
                "kind": "mcp",
                "snapshotId": snapshot.id,
                "digest": snapshot.digest,
                "interfaceKind": "direct",
                "effect": snapshot.side_effect,
            }
        elif kind in {"host-operator", "browser"}:
            ref = dependency.get("ref") or dependency.get("name")
            from app.application.tool_broker import ToolAdapterRegistry

            descriptor = ToolAdapterRegistry().describe(ref)
            locks[alias] = {
                "kind": "tool",
                "capabilityRef": ref,
                "digest": descriptor["capabilityIdentity"],
                "interfaceKind": "direct",
                "effect": descriptor["effect"],
            }
        else:
            raise ValueError(f"{alias}: 不支持的依赖类型 {kind}，不能发布未绑定依赖")
    for interface in manifest["spec"]["interfaces"].values():
        for alias in interface.get("capabilities", []):
            if alias not in locks:
                raise ValueError(f"未声明的依赖 {alias}")
            if interface["kind"] == "direct" and locks[alias]["interfaceKind"] != "direct":
                raise ValueError("Direct 传递依赖不得调用模型")
    return locks
