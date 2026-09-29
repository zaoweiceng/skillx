"""Conservative conflict grouping for effects whose resources are not disjoint."""

from app.infrastructure.database import CapabilityVersion


async def conflicting_nodes(session, definition):
    conflicts = set()
    for node in definition.spec.nodes:
        if node.side_effect or node.type in {"browser", "mcp"}:
            conflicts.add(node.id)
        elif node.type == "agent" and node.config.get("allowedCapabilities"):
            conflicts.add(node.id)
        elif node.type == "skillx":
            version = await session.get(CapabilityVersion, node.config.get("capabilityVersionId"))
            if not version:
                conflicts.add(node.id)
                continue
            interface = version.manifest_json["interfaces"].get(node.config.get("interface"), {})
            if interface.get("effects") or any(
                lock.get("effect", "unknown") not in {"none", "read", "data.read"}
                for lock in version.manifest_json.get("dependencyLocks", {}).values()
            ):
                conflicts.add(node.id)
    return conflicts
