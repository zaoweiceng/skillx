"""Availability is an execution gate, not a change to a published contract."""
from app.infrastructure.models import CapabilityAvailability

async def require_enabled(session, capability_id):
    state = await session.get(CapabilityAvailability, capability_id)
    if state is not None and not state.enabled:
        raise ValueError("该能力已关闭，请先在 SkillX 中启用")


async def require_version_enabled(session, version, seen=None):
    """Check pinned dependencies before admitting a new execution."""
    from app.infrastructure.models import CapabilityVersion, WorkflowVersion
    seen = set() if seen is None else seen
    if version.id in seen:
        return
    seen.add(version.id)
    if isinstance(version, CapabilityVersion):
        await require_enabled(session, version.capability_id)
        return
    await require_enabled(session, version.workflow_id)
    for node in version.definition_json["spec"]["nodes"]:
        if node["type"] not in {"skillx", "capability_call", "workflow_call"}:
            continue
        config = node["config"]
        version_id = config.get("capabilityVersionId") or config.get("workflowVersionId")
        child = await session.get(CapabilityVersion, version_id) or await session.get(WorkflowVersion, version_id)
        if child is not None:
            await require_version_enabled(session, child, seen)
