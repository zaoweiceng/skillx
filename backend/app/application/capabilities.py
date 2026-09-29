from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.secrets import (
    SecretResolutionError,
    resolve_secret_handles,
    secret_names_for_interface,
)
from app.config import get_settings
from app.domain.workflow import NodeType, WorkflowDefinition, WorkflowNode
from app.infrastructure.database import Capability, CapabilityVersion
from app.runtime.skillx import (
    SkillXValidationError,
    load_manifest,
    package_digest,
)


class CapabilityResolutionError(ValueError):
    pass


def resolve_package_path(relative_path: str) -> Path:
    if Path(relative_path).is_absolute():
        raise SkillXValidationError("packagePath 必须是 capability_root 下的相对路径")
    root = get_settings().capability_root.resolve()
    package = (root / relative_path).resolve()
    if not package.is_relative_to(root) or not package.is_dir():
        raise SkillXValidationError("SkillX 包必须位于 capability_root 下")
    return package


def snapshot_manifest(package: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    interfaces: dict[str, Any] = {}
    for name, interface in manifest["spec"]["interfaces"].items():
        interfaces[name] = {
            "kind": interface["kind"],
            "description": interface["description"],
            "title": interface.get("title", name),
            "uiSchema": json.loads((package / interface["uiSchema"]).read_text(encoding="utf-8"))
            if interface.get("uiSchema") else {},
            "inputSchema": json.loads(
                (package / interface["inputSchema"]).read_text(encoding="utf-8")
            ),
            "outputSchema": json.loads(
                (package / interface["outputSchema"]).read_text(encoding="utf-8")
            ),
            "runtime": interface.get("runtime"),
            "execution": interface["execution"],
            "permissions": interface.get("permissions", {}),
            "effects": interface.get("effects", []),
        }
    return {
        "manifest": manifest,
        "interfaces": interfaces,
        "packageDigest": package_digest(package),
        "relativePath": package.name,
    }


async def register_skillx(
    session: AsyncSession,
    relative_path: str,
) -> tuple[Capability, CapabilityVersion]:
    package = resolve_package_path(relative_path)
    manifest = load_manifest(package)
    snapshot = snapshot_manifest(package, manifest)
    from app.application.skillx_dependencies import lock_dependencies

    snapshot["dependencyLocks"] = await lock_dependencies(session, manifest)
    metadata = manifest["metadata"]
    capability = await session.scalar(select(Capability).where(Capability.name == metadata["name"]))
    if capability is None:
        capability = Capability(
            id=str(uuid4()),
            type="skillx",
            name=metadata["name"],
            visibility="private",
        )
        session.add(capability)
        await session.flush()
    existing = await session.scalar(
        select(CapabilityVersion).where(
            CapabilityVersion.capability_id == capability.id,
            CapabilityVersion.version == metadata["version"],
        )
    )
    if existing:
        if existing.digest != snapshot["packageDigest"]:
            raise SkillXValidationError("同一 SemVer 的包内容已变化；请提升版本后重新注册")
        return capability, existing
    import shutil

    frozen = (
        get_settings().capability_root.resolve()
        / ".versions"
        / snapshot["packageDigest"].split(":", 1)[1]
    )
    if not frozen.exists():
        frozen.parent.mkdir(parents=True, exist_ok=True)
        staging = frozen.with_name(frozen.name + "-" + str(uuid4()))
        shutil.copytree(
            package,
            staging,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "node_modules"),
        )
        if package_digest(staging) != snapshot["packageDigest"]:
            raise SkillXValidationError("能力快照复制后 digest 不一致")
        try:
            staging.rename(frozen)
        except FileExistsError:
            shutil.rmtree(staging)
    relative_path = str(frozen.relative_to(get_settings().capability_root.resolve()))
    version = CapabilityVersion(
        id=str(uuid4()),
        capability_id=capability.id,
        version=metadata["version"],
        manifest_json=snapshot,
        digest=snapshot["packageDigest"],
        status="published",
        package_path=relative_path,
    )
    session.add(version)
    await session.flush()
    capability.current_version_id = version.id
    return capability, version


async def validate_workflow_capabilities(
    session: AsyncSession,
    definition: WorkflowDefinition,
) -> tuple[str, list[dict[str, Any]]]:
    deterministic_workflows = get_settings().deterministic_workflows
    execution_class = "zero_llm"
    locks: list[dict[str, Any]] = []
    for node in definition.spec.nodes:
        if deterministic_workflows and node.type in {
            NodeType.LLM,
            NodeType.AGENT,
        }:
            raise CapabilityResolutionError(
                f"确定性工作流不支持 {node.type.value} 节点；请先抽象为已注册 SkillX"
            )
        if node.type == NodeType.LLM:
            execution_class = "hybrid"
        elif node.type == NodeType.AGENT:
            execution_class = "agent"
        if node.type != NodeType.SKILLX:
            continue
        version_id = node.config.get("capabilityVersionId")
        interface_name = node.config.get("interface")
        if not version_id or not interface_name or node.config.get("package"):
            raise CapabilityResolutionError(
                f"SkillX 节点 {node.id} 必须引用 capabilityVersionId + interface"
            )
        version = await session.get(CapabilityVersion, version_id)
        if version is None or version.status != "published":
            raise CapabilityResolutionError(f"SkillX 节点 {node.id} 引用的能力版本不可用")
        interface = version.manifest_json.get("interfaces", {}).get(interface_name)
        if not interface:
            raise CapabilityResolutionError(f"SkillX 节点 {node.id} 的接口 {interface_name} 不存在")
        if deterministic_workflows and interface["kind"] != "direct":
            raise CapabilityResolutionError(
                f"确定性工作流仅支持 direct SkillX 接口，节点 {node.id} 引用了 {interface['kind']}"
            )
        if interface["kind"] == "llm" and execution_class != "agent":
            execution_class = "hybrid"
        elif interface["kind"] == "agent":
            execution_class = "agent"
        package = resolve_package_path(version.package_path or "")
        actual_digest = package_digest(package)
        if actual_digest != version.digest:
            raise CapabilityResolutionError(f"能力版本 {version.id} 内容已变化，请注册新版本")
        locks.append(
            {
                "nodeId": node.id,
                "capabilityVersionId": version.id,
                "interface": interface_name,
                "digest": version.digest,
            }
        )
    return execution_class, locks


async def resolve_skillx_node(
    session: AsyncSession,
    node: WorkflowNode,
) -> WorkflowNode:
    if node.type != NodeType.SKILLX:
        return node
    version_id = node.config.get("capabilityVersionId")
    interface_name = node.config.get("interface")
    version = await session.get(CapabilityVersion, version_id)
    if version is None or version.status != "published":
        raise CapabilityResolutionError("锁定的 CapabilityVersion 不可用")
    if interface_name not in version.manifest_json.get("interfaces", {}):
        raise CapabilityResolutionError("锁定的 SkillX 接口不存在")
    package = resolve_package_path(version.package_path or "")
    if package_digest(package) != version.digest:
        raise CapabilityResolutionError("SkillX 包内容与锁定 digest 不一致")
    try:
        secret_names = secret_names_for_interface(
            version.manifest_json["manifest"],
            interface_name,
        )
        secrets = await resolve_secret_handles(session, secret_names)
    except SecretResolutionError as exc:
        raise CapabilityResolutionError(str(exc)) from exc
    config = {
        **node.config,
        "_resolvedPackage": str(package),
        "_resolvedSecrets": secrets,
    }
    return node.model_copy(update={"config": config}, deep=True)
