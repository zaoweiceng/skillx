"""Publication may change implementations, never silently widen effect/Host grants."""

from app.application.compiler import digest_json
from app.infrastructure.database import CapabilityVersion


def dependency_scope(dependency):
    value = dict(dependency)
    # A pinned implementation update does not change the named capability/interface grant.
    if value.get("kind") == "skillx":
        name_version, _, interface = value.get("ref", "").partition("#")
        name, _, _version = name_version.rpartition("@")
        value["ref"] = name + "#" + interface
    return value


async def authorization_grants(session, definition):
    grants = set()
    for node in definition["spec"]["nodes"]:
        if node.get("permissions"):
            grants.add("node-permissions:" + digest_json(node["permissions"]))
        if node.get("sideEffect"):
            grants.add("side-effect:" + node["type"])
        if node["type"] in {"llm", "agent"}:
            grants.add("model.invoke")
            grants.update(
                "host:" + name for name in node.get("config", {}).get("allowedCapabilities", [])
            )
        if node["type"] != "skillx":
            continue
        version = await session.get(CapabilityVersion, node["config"]["capabilityVersionId"])
        if not version:
            raise ValueError("权限比较缺少锁定的 SkillX 版本")
        spec = version.manifest_json["manifest"]["spec"]
        interface = spec["interfaces"][node["config"]["interface"]]
        if interface["kind"] != "direct":
            grants.add("model.invoke")
        for permissions in [spec.get("permissions"), interface.get("permissions")]:
            if permissions:
                grants.add("skill-permissions:" + digest_json(permissions))
        for effect in interface.get("effects", []):
            grants.add("effect:" + digest_json(effect))
        for alias in interface.get("capabilities", []):
            grants.add("dependency:" + digest_json(dependency_scope(spec["capabilities"][alias])))
    return grants


async def verify_repair_authorization(session, before, after):
    if not (await authorization_grants(session, after)).issubset(
        await authorization_grants(session, before)
    ):
        raise ValueError("候选修订扩大权限或模型/副作用范围，不能自动激活")
