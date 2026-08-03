from __future__ import annotations

import importlib.util
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from app.domain.workflow import ExecutionError, NodeResult

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillXValidationError(ValueError):
    pass


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


@dataclass
class SkillXContext:
    capabilities: DeniedCapabilityClient = field(default_factory=DeniedCapabilityClient)

    def check_cancelled(self) -> None:
        return None


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


def load_interface(package_root: Path, interface_name: str) -> LoadedInterface:
    manifest_file = _safe_file(package_root, "skillx.yaml")
    _safe_file(package_root, "SKILL.md")
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SkillXValidationError("skillx.yaml 必须是对象")
    if manifest.get("apiVersion") != "skillx.dev/v1alpha1":
        raise SkillXValidationError("不支持的 SkillX apiVersion")
    if manifest.get("kind") != "SkillX":
        raise SkillXValidationError("kind 必须为 SkillX")
    metadata = manifest.get("metadata", {})
    name = metadata.get("name", "")
    if not NAME_PATTERN.fullmatch(name):
        raise SkillXValidationError("metadata.name 不符合命名规范")
    version = metadata.get("version")
    if not isinstance(version, str) or not version:
        raise SkillXValidationError("metadata.version 不能为空")
    spec = manifest.get("spec", {})
    interfaces = spec.get("interfaces", {})
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
        file_name, callable_name = runtime["entry"].split(":", 1)
        function = getattr(_load_module(_safe_file(loaded.package_root, file_name)), callable_name)
        request = SkillXRequest(
            invocation_id=invocation_id,
            interface=loaded.interface_name,
            input=inputs,
        )
        result = function(request, SkillXContext())
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            result = SkillXResult(output=result)
        if not isinstance(result, SkillXResult):
            raise TypeError("执行器必须返回 SkillXResult 或 dict")
        Draft202012Validator(loaded.output_schema).validate(result.output)
        return NodeResult(status="succeeded", output=result.output, metrics=result.metrics)
    except PermissionError as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(code="PERMISSION_DENIED", message=str(exc)),
        )
    except Exception as exc:
        return NodeResult(
            status="failed",
            error=ExecutionError(
                code="CONTRACT_VIOLATION",
                message=f"SkillX 执行失败: {exc}",
            ),
        )
