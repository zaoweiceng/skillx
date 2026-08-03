from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from app.domain.workflow import ExecutionError, NodeResult

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
TOP_LEVEL_FIELDS = {"apiVersion", "kind", "metadata", "spec"}
SPEC_FIELDS = {
    "guide",
    "compatibility",
    "defaultInterface",
    "interfaces",
    "capabilities",
    "environments",
    "permissions",
    "policies",
}
INTERFACE_FIELDS = {
    "kind",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "uiSchema",
    "examples",
    "capabilities",
    "execution",
    "permissions",
    "effects",
    "artifacts",
    "runtime",
    "prompt",
    "model",
    "agent",
}


class SkillXValidationError(ValueError):
    pass


class SkillXExecutionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class SkillXRequest:
    invocation_id: str
    interface: str
    input: dict[str, Any]
    idempotency_key: str | None = None
    locale: str | None = "zh-CN"


@dataclass
class SkillXResult:
    output: dict[str, Any]
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, int | float | str] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)


class DeniedCapabilityClient:
    async def call(self, capability: str, arguments: dict[str, Any]) -> Any:
        raise PermissionError(f"能力 {capability} 未绑定运行时适配器")


class SecretAccessor:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError as exc:
            raise PermissionError(f"Secret {name} 未声明或不可用") from exc


@dataclass
class SkillXContext:
    capabilities: DeniedCapabilityClient = field(default_factory=DeniedCapabilityClient)
    secrets: SecretAccessor = field(default_factory=SecretAccessor)
    cancellation_check: Callable[[], None] = lambda: None

    def check_cancelled(self) -> None:
        self.cancellation_check()


@dataclass(frozen=True)
class LoadedInterface:
    package_root: Path
    manifest: dict[str, Any]
    name: str
    version: str
    interface_name: str
    interface: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


def _safe_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root = root.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise SkillXValidationError(f"非法或不存在的包内路径: {relative}")
    return candidate


def _reject_unknown(value: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = [key for key in value if key not in allowed and not key.startswith("x-")]
    if unknown:
        raise SkillXValidationError(f"{path} 包含未知字段: {', '.join(sorted(unknown))}")


def _skill_name(skill_file: Path) -> str | None:
    content = skill_file.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None
    _, frontmatter, _ = content.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    return parsed.get("name") if isinstance(parsed, dict) else None


def load_manifest(package_root: Path) -> dict[str, Any]:
    manifest_file = _safe_file(package_root, "skillx.yaml")
    skill_file = _safe_file(package_root, "SKILL.md")
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SkillXValidationError("skillx.yaml 必须是对象")
    _reject_unknown(manifest, TOP_LEVEL_FIELDS, "manifest")
    if manifest.get("apiVersion") != "skillx.dev/v1alpha1":
        raise SkillXValidationError("不支持的 SkillX apiVersion")
    if manifest.get("kind") != "SkillX":
        raise SkillXValidationError("kind 必须为 SkillX")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SkillXValidationError("metadata 必须为对象")
    name = metadata.get("name", "")
    if not NAME_PATTERN.fullmatch(name):
        raise SkillXValidationError("metadata.name 不符合命名规范")
    if _skill_name(skill_file) != name:
        raise SkillXValidationError("SKILL.md name 必须与 metadata.name 一致")
    version = metadata.get("version")
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        raise SkillXValidationError("metadata.version 必须为 SemVer")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise SkillXValidationError("metadata.description 不能为空")
    spec = manifest.get("spec", {})
    if not isinstance(spec, dict):
        raise SkillXValidationError("spec 必须为对象")
    _reject_unknown(spec, SPEC_FIELDS, "spec")
    guide = spec.get("guide")
    if not isinstance(guide, dict) or guide.get("entry") != "SKILL.md":
        raise SkillXValidationError("spec.guide.entry 必须为 SKILL.md")
    interfaces = spec.get("interfaces", {})
    if not isinstance(interfaces, dict) or not interfaces:
        raise SkillXValidationError("spec.interfaces 不能为空")
    default_interface = spec.get("defaultInterface")
    if default_interface not in interfaces:
        raise SkillXValidationError("spec.defaultInterface 必须指向已定义接口")
    package_secrets = spec.get("permissions", {}).get("secrets", {}).get("use", [])
    if not isinstance(package_secrets, list) or not all(
        isinstance(item, str) and item for item in package_secrets
    ):
        raise SkillXValidationError("spec.permissions.secrets.use 必须为非空字符串数组")
    for interface_name, interface in interfaces.items():
        if not NAME_PATTERN.fullmatch(interface_name) or not isinstance(interface, dict):
            raise SkillXValidationError(f"接口名称无效: {interface_name}")
        _reject_unknown(interface, INTERFACE_FIELDS, f"spec.interfaces.{interface_name}")
        for required in ("kind", "description", "inputSchema", "outputSchema", "execution"):
            if required not in interface:
                raise SkillXValidationError(f"接口 {interface_name} 缺少 {required}")
        if interface["kind"] not in {"direct", "llm", "agent"}:
            raise SkillXValidationError(f"接口 {interface_name} kind 无效")
        interface_secret_policy = interface.get("permissions", {}).get("secrets")
        if isinstance(interface_secret_policy, dict):
            interface_secrets = interface_secret_policy.get("use", [])
            if not isinstance(interface_secrets, list) or not all(
                isinstance(item, str) and item for item in interface_secrets
            ):
                raise SkillXValidationError(
                    f"接口 {interface_name} permissions.secrets.use 无效"
                )
            undeclared = sorted(set(interface_secrets) - set(package_secrets))
            if undeclared:
                raise SkillXValidationError(
                    f"接口 {interface_name} 使用未在包级声明的 Secret: "
                    + ", ".join(undeclared)
                )
        input_schema = json.loads(
            _safe_file(package_root, interface["inputSchema"]).read_text(encoding="utf-8")
        )
        output_schema = json.loads(
            _safe_file(package_root, interface["outputSchema"]).read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
        if input_schema.get("type") != "object" or output_schema.get("type") != "object":
            raise SkillXValidationError("接口输入输出 Schema 根节点必须为 object")
        if interface["kind"] == "direct":
            runtime = interface.get("runtime")
            if not isinstance(runtime, dict) or not runtime.get("entry"):
                raise SkillXValidationError(f"Direct 接口 {interface_name} 缺少 runtime")
            file_name, _, callable_name = runtime["entry"].partition(":")
            if not file_name or not callable_name:
                raise SkillXValidationError("Python callable 入口格式无效")
            _safe_file(package_root, file_name)
    return manifest


def package_digest(package_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name.startswith(".")
        ):
            continue
        relative = path.relative_to(package_root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def load_interface(package_root: Path, interface_name: str) -> LoadedInterface:
    manifest = load_manifest(package_root)
    metadata = manifest["metadata"]
    name = metadata["name"]
    version = metadata["version"]
    interfaces = manifest["spec"]["interfaces"]
    interface = interfaces.get(interface_name)
    if not isinstance(interface, dict):
        raise SkillXValidationError(f"接口 {interface_name} 不存在")
    if interface.get("kind") != "direct":
        raise SkillXValidationError("当前运行时仅执行 direct 接口")
    input_schema = json.loads(
        _safe_file(package_root, interface["inputSchema"]).read_text(encoding="utf-8")
    )
    output_schema = json.loads(
        _safe_file(package_root, interface["outputSchema"]).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(input_schema)
    Draft202012Validator.check_schema(output_schema)
    return LoadedInterface(
        package_root=package_root.resolve(),
        manifest=manifest,
        name=name,
        version=version,
        interface_name=interface_name,
        interface=interface,
        input_schema=input_schema,
        output_schema=output_schema,
    )


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"skillx_{path.stem}_{path.stat().st_mtime_ns}", path
    )
    if spec is None or spec.loader is None:
        raise SkillXValidationError(f"无法加载执行器 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def execute_direct(
    loaded: LoadedInterface,
    invocation_id: str,
    inputs: dict[str, Any],
    cancellation_check: Callable[[], None] = lambda: None,
    secrets: dict[str, str] | None = None,
) -> NodeResult:
    input_errors = sorted(
        Draft202012Validator(loaded.input_schema).iter_errors(inputs),
        key=lambda item: list(item.path),
    )
    if input_errors:
        error = input_errors[0]
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="INVALID_INPUT",
                message=error.message,
                details={"path": "/" + "/".join(str(part) for part in error.path)},
            ),
        )
    runtime = loaded.interface.get("runtime", {})
    if runtime.get("protocol") != "skillx.python-callable/v1":
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="UNSUPPORTED_OPERATION",
                message="当前 Host 仅支持 skillx.python-callable/v1",
            ),
        )
    try:
        cancellation_check()
        file_name, callable_name = runtime["entry"].split(":", 1)
        function = getattr(_load_module(_safe_file(loaded.package_root, file_name)), callable_name)
        request = SkillXRequest(
            invocation_id=invocation_id,
            interface=loaded.interface_name,
            input=inputs,
        )
        result = function(
            request,
            SkillXContext(
                secrets=SecretAccessor(secrets),
                cancellation_check=cancellation_check,
            ),
        )
        if inspect.isawaitable(result):
            result = await result
        cancellation_check()
        if isinstance(result, dict):
            result = SkillXResult(output=result)
        if not isinstance(result, SkillXResult):
            raise TypeError("执行器必须返回 SkillXResult 或 dict")
        Draft202012Validator(loaded.output_schema).validate(result.output)
        return NodeResult(status="succeeded", output=result.output, metrics=result.metrics)
    except SkillXExecutionError as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
            ),
        )
    except PermissionError as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="PERMISSION_DENIED", message=str(exc)),
        )
    except JsonSchemaValidationError as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="CONTRACT_VIOLATION", message=exc.message),
        )
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="EXECUTION_FAILED",
                message=f"SkillX 执行失败: {exc}",
            ),
        )
