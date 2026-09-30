"""Public capability contracts; implementation selection belongs to the Host.

Existing immutable version IDs remain valid. No second mutable registry or copy of
the workflow is created. Internal invocation kinds remain available for execution
and learning, but are never required by a caller.
"""

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select

from app.infrastructure.database import (
    Capability,
    CapabilityVersion,
    Workflow,
    WorkflowCapabilityManifest,
    WorkflowVersion,
)

PUBLIC_INTERFACE_FIELDS = (
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "uiSchema",
    "examples",
    "execution",
    "permissions",
    "effects",
    "artifacts",
)


def merge_requirements(left: dict, right: dict) -> dict:
    """Conservative union for discovery only; this never grants permissions."""
    result = deepcopy(left)
    for key, value in right.items():
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = merge_requirements(old, value)
        elif isinstance(old, list) and isinstance(value, list):
            result[key] = old + [deepcopy(item) for item in value if item not in old]
        elif isinstance(old, bool) and isinstance(value, bool):
            result[key] = old or value
        else:
            result[key] = deepcopy(value)
    return result


@dataclass(frozen=True)
class CallableCapability:
    implementation: str
    contract: dict

    def bind(self, interface: str, inputs: dict) -> tuple[str, dict]:
        if interface not in self.contract["interfaces"]:
            raise ValueError("能力接口不存在")
        arguments = {"versionId": self.contract["id"], "input": inputs}
        if self.implementation == "skillx":
            arguments["interface"] = interface
        return self.implementation, arguments


async def resolve_callable(session, version_id: str) -> CallableCapability:
    skill = await session.get(CapabilityVersion, version_id)
    workflow = await session.get(WorkflowVersion, version_id)
    if skill is not None and workflow is not None:
        raise ValueError("能力版本标识冲突")
    if skill is not None:
        if skill.status != "published":
            raise ValueError("能力发布版本不存在")
        owner = await session.get(Capability, skill.capability_id)
        interfaces = {
            name: {key: deepcopy(value[key]) for key in PUBLIC_INTERFACE_FIELDS if key in value}
            for name, value in skill.manifest_json["interfaces"].items()
        }
        source_interfaces = (
            skill.manifest_json.get("manifest", {}).get("spec", {}).get("interfaces", {})
        )
        for name, contract in interfaces.items():
            contract.setdefault(
                "description", source_interfaces.get(name, {}).get("description", owner.name)
            )
            contract["permissions"] = merge_requirements(
                skill.manifest_json.get("manifest", {}).get("spec", {}).get("permissions", {}),
                contract.get("permissions", {}),
            )
            if skill.manifest_json["interfaces"][name].get("kind") in {"llm", "agent"}:
                contract["permissions"] = merge_requirements(
                    contract["permissions"],
                    {"model": {"invoke": True}},
                )
        metadata = skill.manifest_json.get("manifest", {}).get("metadata", {})
        return CallableCapability(
            "skillx",
            {
                "id": skill.id,
                "capabilityId": skill.capability_id,
                "name": owner.name,
                "displayName": metadata.get("displayName") or owner.name,
                "description": metadata.get("description", ""),
                "version": skill.version,
                "status": "published",
                "digest": skill.digest,
                "interfaces": interfaces,
            },
        )
    if workflow is None:
        raise ValueError("能力发布版本不存在")
    definition = workflow.definition_json
    spec = definition["spec"]
    semantic = definition.get("metadata", {}).get("capability", {})
    owner = await session.get(Workflow, workflow.workflow_id)
    effects, permissions = [], {}
    idempotent = True
    for node in spec["nodes"]:
        permissions = merge_requirements(permissions, {"required": node.get("permissions", [])})
        idempotent = idempotent and node.get("idempotent", True)
        if node["type"] == "skillx":
            dependency = await resolve_callable(session, node["config"]["capabilityVersionId"])
            contract = dependency.contract["interfaces"][node["config"]["interface"]]
            effects.extend(
                {**deepcopy(effect), "id": node["id"] + ":" + effect.get("id", "effect")}
                for effect in contract.get("effects", [])
            )
            permissions = merge_requirements(permissions, contract.get("permissions", {}))
            idempotent = (
                idempotent and contract.get("execution", {}).get("idempotency") == "inherent"
            )
        elif node["type"] == "approval":
            effects.append({"id": node["id"], "type": "approval", "confirmation": "required"})
        elif node.get("sideEffect"):
            effects.append({"id": node["id"], "type": "declared", "confirmation": "runtime"})
        if node["type"] in {"llm", "agent"}:
            permissions = merge_requirements(permissions, {"model": {"invoke": True}})
    return CallableCapability(
        "workflow",
        {
            "id": workflow.id,
            "capabilityId": workflow.workflow_id,
            "name": owner.name,
            "displayName": owner.name,
            "description": semantic.get("summary") or definition.get("metadata", {}).get("description", ""),
            "version": str(workflow.version),
            "status": "published",
            "digest": workflow.digest,
            "interfaces": {
                "execute": {
                    "description": semantic.get("summary", owner.name),
                    "inputSchema": deepcopy(spec["inputSchema"]),
                    "outputSchema": deepcopy(spec["outputSchema"]),
                    "uiSchema": deepcopy(spec.get("uiSchema", {})),
                    "execution": {
                        "timeoutSeconds": spec.get("policies", {}).get("maxDurationSeconds", 300),
                        "idempotency": "inherent" if idempotent else "none",
                    },
                    "permissions": permissions,
                    "effects": effects,
                }
            },
            **{
                key: deepcopy(semantic[key])
                for key in ("useWhen", "doNotUseWhen", "examples", "tags")
                if key in semantic
            },
        },
    )


async def callable_catalog(session) -> list[dict]:
    version_ids = list(
        await session.scalars(
            select(Capability.current_version_id).where(Capability.current_version_id.is_not(None))
        )
    )
    version_ids.extend(
        await session.scalars(
            select(WorkflowCapabilityManifest.workflow_version_id).where(
                WorkflowCapabilityManifest.status.in_(["published", "active"])
            )
        )
    )
    result = []
    for version_id in dict.fromkeys(version_ids):
        result.append((await resolve_callable(session, version_id)).contract)
    return result


async def callable_versions(session, capability_id: str) -> list[dict]:
    skills = list(
        await session.scalars(
            select(CapabilityVersion.id)
            .where(
                CapabilityVersion.capability_id == capability_id,
                CapabilityVersion.status == "published",
            )
            .order_by(CapabilityVersion.created_at.desc())
        )
    )
    workflows = list(
        await session.scalars(
            select(WorkflowVersion.id)
            .where(WorkflowVersion.workflow_id == capability_id)
            .order_by(WorkflowVersion.version.desc())
        )
    )
    if skills and workflows:
        raise ValueError("能力标识冲突")
    return [(await resolve_callable(session, item)).contract for item in skills + workflows]


def public_call(kind: str, arguments: dict) -> tuple[str, dict]:
    if kind not in {"workflow", "skillx"}:
        return kind, arguments
    return "capability", {
        "capabilityVersionId": arguments.get("versionId"),
        "interface": arguments.get("interface", "execute"),
        "input": arguments.get("input", {}),
    }


def public_error(error: dict | None) -> dict | None:
    """Project both Run error envelopes and executor errors to one contract."""
    if not error:
        return None
    details = deepcopy(error.get("details", {}))
    if error.get("technicalDetailRef"):
        details["diagnosticRef"] = error["technicalDetailRef"]
    return {
        "code": error.get("code", "INTERNAL_ERROR"),
        "message": error.get("userMessage") or error.get("message") or "能力执行失败",
        "retryable": bool(error.get("retryable", False)),
        "details": details,
    }
