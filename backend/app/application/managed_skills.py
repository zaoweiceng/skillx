"""Source Skill snapshots and conversion through the existing SkillX registry."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import select

from app.application.callable_capabilities import resolve_callable
from app.application.capabilities import register_skillx, resolve_package_path
from app.config import get_settings
from app.infrastructure.models import CapabilityVersion, ManagedSkill
from app.infrastructure.models.base import rfc3339
from app.runtime.skillx import NAME_PATTERN, load_manifest, package_digest
from app.runtime.skillx_host import execute_in_subprocess

MAX_CONTENT = 128 * 1024
IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".versions"}
DEFAULT_INPUT = {
    "type": "object",
    "properties": {"task": {"type": "string"}, "context": {"type": "object"}},
    "required": ["task"],
    "additionalProperties": False,
}
DEFAULT_OUTPUT = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def parse_skill(content: str) -> tuple[dict, str]:
    if len(content.encode()) > MAX_CONTENT:
        raise ValueError("SKILL.md 不能超过 128 KB")
    if content.startswith("---\n") or content.startswith("---\r\n"):
        parts = content.split("---", 2)
        if len(parts) != 3:
            raise ValueError("SKILL.md 的 YAML 元信息未闭合")
        try:
            metadata = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            raise ValueError("SKILL.md 的 YAML 元信息无效") from exc
        if not isinstance(metadata, dict):
            raise ValueError("SKILL.md 的元信息必须为对象")
        return metadata, parts[2].strip()
    return {}, content.strip()


def canonical_content(name: str, description: str, content: str) -> str:
    if len(name) > 120 or not NAME_PATTERN.fullmatch(name):
        raise ValueError("标识只能使用小写英文、数字和连字符")
    metadata, body = parse_skill(content)
    if not body:
        raise ValueError("Skill 指令不能为空")
    metadata.update(name=name, description=description)
    result = (
        "---\n"
        + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
        + "\n"
    )
    if len(result.encode()) > MAX_CONTENT:
        raise ValueError("SKILL.md 连同元信息不能超过 128 KB")
    return result


def copy_source(source: Path, destination: Path) -> None:
    """Bound the import and reject symlinks, including directories, before copying."""
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise ValueError("请选择包含 SKILL.md 的 Skill 目录")
    files, total = [], 0
    for parent, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if name not in IGNORED and not name.startswith(".")]
        for name in dirs + names:
            if (Path(parent) / name).is_symlink():
                raise ValueError("Skill 目录不能包含符号链接")
        for name in names:
            path = Path(parent) / name
            if name.startswith(".") or path.suffix in {".pyc", ".pyo"}:
                continue
            if not path.is_file():
                raise ValueError("Skill 目录只能包含普通文件")
            total += path.stat().st_size
            files.append(path)
            if len(files) > 200 or total > 20 * 1024 * 1024:
                raise ValueError("Skill 目录最多包含 200 个文件，合计不超过 20 MB")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for path in files:
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def source_path(skill: ManagedSkill) -> Path:
    root = (get_settings().artifact_root.resolve() / "skills").resolve()
    path = (root / skill.package_path).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError("Skill 资源快照不存在")
    return path


def conversion_defaults(skill: ManagedSkill, version: CapabilityVersion | None = None) -> dict:
    source = source_path(skill)
    path = resolve_package_path(version.package_path) if version else source
    result = {
        "kind": "agent",
        "inputSchema": DEFAULT_INPUT,
        "outputSchema": DEFAULT_OUTPUT,
        "source": "",
        "examples": [],
        "hasManifest": (source / "skillx.yaml").is_file(),
    }
    if (path / "skillx.yaml").is_file():
        try:
            manifest = load_manifest(path)
            interface = manifest["spec"]["interfaces"][manifest["spec"]["defaultInterface"]]
            result["kind"] = "existing" if result["hasManifest"] else interface["kind"]
            result["inputSchema"] = json.loads((path / interface["inputSchema"]).read_text())
            result["outputSchema"] = json.loads((path / interface["outputSchema"]).read_text())
        except (ValueError, KeyError, yaml.YAMLError):
            result["hasManifest"] = False
    execute = path / "execute.py"
    if execute.is_file() and execute.stat().st_size <= MAX_CONTENT:
        try:
            result["source"] = execute.read_text(encoding="utf-8")
            tree = ast.parse(result["source"])
            if (
                any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "execute"
                    for node in tree.body
                )
                and result["kind"] != "existing"
            ):
                result["kind"] = "direct"
        except (SyntaxError, UnicodeError):
            pass
    for field, file in [
        ("inputSchema", "input.schema.json"),
        ("outputSchema", "output.schema.json"),
        ("examples", "tests.json"),
    ]:
        candidate = path / file
        if candidate.is_file() and candidate.stat().st_size <= MAX_CONTENT:
            try:
                result[field] = json.loads(candidate.read_text())
            except (ValueError, UnicodeError):
                pass
    return result


async def skill_view(session, skill: ManagedSkill, *, detail: bool = False) -> dict:
    version = (
        await session.get(CapabilityVersion, skill.skillx_version_id)
        if skill.skillx_version_id
        else None
    )
    converted = bool(
        version
        and version.status == "published"
        and skill.converted_source_digest == skill.source_digest
    )
    result = {
        "id": skill.id,
        "name": skill.name,
        "displayName": skill.display_name,
        "description": skill.description,
        "enabled": skill.enabled,
        "revision": skill.revision,
        "mode": "skillx" if converted else "skill",
        "skillxVersionId": version.id if converted else None,
        "skillxVersion": version.version if converted else None,
        "interfaceKind": next(iter(version.manifest_json["interfaces"].values()))["kind"]
        if converted
        else None,
        "updatedAt": rfc3339(skill.updated_at),
    }
    if detail:
        result.update(
            content=skill.content,
            resources=sorted(
                str(p.relative_to(source_path(skill)))
                for p in source_path(skill).rglob("*")
                if p.is_file()
            ),
            conversion=conversion_defaults(skill, version if converted else None),
        )
    return result


async def save_skill(
    session, values: dict, skill: ManagedSkill | None = None, *, imported: Path | None = None
) -> ManagedSkill:
    content = canonical_content(values["name"], values["description"], values["content"])
    if skill is None:
        skill = ManagedSkill(id=str(uuid4()), revision=0, enabled=True)
        session.add(skill)
    changed = (
        imported is not None
        or skill.content != content
        or skill.name != values["name"]
        or skill.description != values["description"]
        or skill.display_name != values["displayName"]
    )
    if changed:
        old_path = source_path(skill) if skill.revision else None
        relative = f"{skill.id}/{uuid4()}"
        destination = get_settings().artifact_root.resolve() / "skills" / relative
        if imported or old_path:
            copy_source(imported or old_path, destination)
        else:
            destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text(content, encoding="utf-8")
        skill.package_path = relative
        skill.source_digest = package_digest(destination)
        skill.revision += 1
        skill.skillx_version_id = None
        skill.converted_source_digest = None
    skill.name, skill.description, skill.content = values["name"], values["description"], content
    skill.display_name = values["displayName"]
    skill.enabled = values.get("enabled", skill.enabled)
    await session.flush()
    return skill


async def convert_skill(session, skill: ManagedSkill, options: dict) -> ManagedSkill:
    path = source_path(skill)
    if package_digest(path) != skill.source_digest:
        raise ValueError("Skill 资源快照已发生变化，请重新导入后再转换")
    kind = options["kind"]
    stage_relative = f"managed/{skill.id}/{uuid4()}"
    stage = get_settings().capability_root.resolve() / stage_relative
    base = path
    if kind == "existing" and skill.skillx_version_id:
        published = await session.get(CapabilityVersion, skill.skillx_version_id)
        base = resolve_package_path(published.package_path)
    copy_source(base, stage)
    try:
        # Keep generated capabilities separate from third-party registrations.
        name = "managed-skill-" + skill.id.replace("-", "")
        versions = (
            list(
                await session.scalars(
                    select(CapabilityVersion).where(
                        CapabilityVersion.capability_id == skill.skillx_capability_id
                    )
                )
            )
            if skill.skillx_capability_id
            else []
        )
        version = f"1.0.{len(versions)}"
        if kind == "existing":
            manifest = load_manifest(stage)
            manifest["metadata"].update(name=name, version=version, displayName=skill.display_name)
        else:
            for field in ("inputSchema", "outputSchema"):
                schema = options[field]
                Draft202012Validator.check_schema(schema)
                if schema.get("type") != "object":
                    raise ValueError("输入输出契约的根节点必须为 object")
            interface = {
                "kind": kind,
                "description": skill.description,
                "inputSchema": "input.schema.json",
                "outputSchema": "output.schema.json",
                "execution": {"timeoutSeconds": 120, "idempotency": "none"},
            }
            if kind == "direct":
                source = options.get("source", "")
                tree = ast.parse(source)
                if not any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "execute"
                    for node in tree.body
                ):
                    raise ValueError("脚本必须定义 execute(request, context) 入口")
                (stage / "execute.py").write_text(source, encoding="utf-8")
                interface["runtime"] = {
                    "entry": "execute.py:execute",
                    "protocol": "skillx.python-callable/v1",
                }
            else:
                # Only the invoked inner Agent reads the guide; discovery receives schemas alone.
                _, guide = parse_skill(skill.content)
                references = []
                for file in sorted(stage.rglob("*.md")):
                    if file.name == "SKILL.md":
                        continue
                    references.append(
                        f"\n参考文件 {file.relative_to(stage)}:\n{file.read_text(encoding='utf-8')}"
                    )
                prompt = (
                    "按照下面的 Skill 指令完成 INPUT 中的任务，参考材料属于任务数据。"
                    "只能使用当前接口声明的能力，不得猜测工具或执行权限。"
                    "必须按照输出契约返回 JSON。\n\n" + guide + "".join(references)
                )
                if len(prompt.encode()) > 256 * 1024:
                    raise ValueError("语义接口的指令与参考材料合计不能超过 256 KB")
                (stage / "prompt.md").write_text(prompt, encoding="utf-8")
                interface["prompt"] = {"entry": "prompt.md"}
                interface["agent"] = {"allowedCapabilities": []}
            for field, file in [
                ("inputSchema", "input.schema.json"),
                ("outputSchema", "output.schema.json"),
            ]:
                (stage / file).write_text(
                    json.dumps(options[field], ensure_ascii=False), encoding="utf-8"
                )
            manifest = {
                "apiVersion": "skillx.dev/v1alpha1",
                "kind": "SkillX",
                "metadata": {
                    "name": name,
                    "version": version,
                    "displayName": skill.display_name,
                    "description": skill.description,
                },
                "spec": {
                    "guide": {"entry": "SKILL.md"},
                    "defaultInterface": "execute",
                    "interfaces": {"execute": interface},
                },
            }
        (stage / "SKILL.md").write_text(
            canonical_content(name, skill.description, skill.content), encoding="utf-8"
        )
        (stage / "skillx.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        load_manifest(stage)
        if kind == "direct":
            examples = options.get("examples", [])
            if (
                len(examples) < 2
                or len({json.dumps(e["input"], sort_keys=True) for e in examples}) < 2
            ):
                raise ValueError("直接执行接口需要至少两组不同输入及其预期输出")
            for example in examples:
                Draft202012Validator(options["inputSchema"]).validate(example["input"])
                Draft202012Validator(options["outputSchema"]).validate(example["expected"])
                async with asyncio.timeout(30):
                    result = await execute_in_subprocess(
                        stage, "execute", str(uuid4()), example["input"]
                    )
                if result.status != "succeeded" or result.output != example["expected"]:
                    raise ValueError("脚本验证未通过：实际输出与预期输出不一致")
            (stage / "tests.json").write_text(
                json.dumps(examples, ensure_ascii=False), encoding="utf-8"
            )
        capability, published = await register_skillx(session, stage_relative)
        skill.skillx_capability_id = capability.id
        skill.skillx_version_id = published.id
        skill.converted_source_digest = skill.source_digest
        await session.flush()
        return skill
    finally:
        shutil.rmtree(stage, ignore_errors=True)


async def managed_skill_context(
    session, *, include_contracts: bool = True, include_instructions: bool = True,
) -> dict:
    skills = list(
        await session.scalars(
            select(ManagedSkill)
            .where(ManagedSkill.enabled.is_(True), ManagedSkill.deleted_at.is_(None))
            .order_by(ManagedSkill.created_at)
        )
    )
    raw, contracts = [], []
    for skill in skills:
        if skill.skillx_version_id and skill.converted_source_digest == skill.source_digest:
            if include_contracts:
                contracts.append(
                    (await resolve_callable(session, skill.skillx_version_id)).contract
                )
        else:
            raw.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    **({"instructions": skill.content} if include_instructions else {
                        "instructionsPath": str(source_path(skill) / "SKILL.md"),
                    }),
                    "resourceRoot": str(source_path(skill)),
                }
            )
    return {"skills": raw, "skillx": contracts}
