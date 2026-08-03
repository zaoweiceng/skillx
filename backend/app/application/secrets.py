from __future__ import annotations

import os
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import SecretReference

SECRET_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,119}$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SecretResolutionError(ValueError):
    pass


def validate_secret_name(name: str) -> None:
    if not SECRET_NAME.fullmatch(name):
        raise SecretResolutionError("Secret 名称格式无效")


async def register_secret_reference(
    session: AsyncSession,
    name: str,
    environment_variable: str,
    description: str = "",
) -> SecretReference:
    validate_secret_name(name)
    if not ENV_NAME.fullmatch(environment_variable):
        raise SecretResolutionError("环境变量名称格式无效")
    existing = await session.scalar(
        select(SecretReference).where(SecretReference.name == name)
    )
    if existing is not None:
        if existing.external_key != environment_variable:
            raise SecretResolutionError("同名 Secret 不得静默更换环境变量")
        return existing
    reference = SecretReference(
        id=str(uuid4()),
        name=name,
        provider="env",
        external_key=environment_variable,
        description=description,
    )
    session.add(reference)
    await session.flush()
    return reference


async def resolve_secret_handles(
    session: AsyncSession,
    names: list[str],
) -> dict[str, str]:
    unique_names = sorted(set(names))
    if not unique_names:
        return {}
    for name in unique_names:
        validate_secret_name(name)
    references = (
        await session.scalars(
            select(SecretReference).where(SecretReference.name.in_(unique_names))
        )
    ).all()
    by_name = {item.name: item for item in references}
    missing = [name for name in unique_names if name not in by_name]
    if missing:
        raise SecretResolutionError(
            "Secret 引用未注册: " + ", ".join(missing)
        )
    resolved: dict[str, str] = {}
    unavailable: list[str] = []
    for name in unique_names:
        value = os.getenv(by_name[name].external_key)
        if value is None:
            unavailable.append(name)
        else:
            resolved[name] = value
    if unavailable:
        raise SecretResolutionError(
            "Secret 对应环境变量不可用: " + ", ".join(unavailable)
        )
    return resolved


def secret_names_for_interface(
    manifest: dict[str, Any],
    interface_name: str,
) -> list[str]:
    spec = manifest.get("spec", {})
    package_use = (
        spec.get("permissions", {}).get("secrets", {}).get("use", [])
    )
    interface = spec.get("interfaces", {}).get(interface_name, {})
    interface_secrets = interface.get("permissions", {}).get("secrets")
    requested = (
        interface_secrets.get("use", [])
        if isinstance(interface_secrets, dict)
        else package_use
    )
    if not isinstance(package_use, list) or not all(
        isinstance(item, str) for item in package_use
    ):
        raise SecretResolutionError("Manifest permissions.secrets.use 必须为字符串数组")
    if not isinstance(requested, list) or not all(
        isinstance(item, str) for item in requested
    ):
        raise SecretResolutionError("接口 permissions.secrets.use 必须为字符串数组")
    undeclared = sorted(set(requested) - set(package_use))
    if undeclared:
        raise SecretResolutionError(
            "接口请求了包级未声明 Secret: " + ", ".join(undeclared)
        )
    return sorted(set(requested))


def contains_secret(value: Any, secret_values: list[str]) -> bool:
    candidates = [item for item in secret_values if item]
    if not candidates:
        return False
    if isinstance(value, dict):
        return any(
            contains_secret(key, candidates) or contains_secret(item, candidates)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_secret(item, candidates) for item in value)
    if isinstance(value, str):
        return any(secret in value for secret in candidates)
    return False


def redact_secret_text(value: str, secret_values: list[str]) -> str:
    for secret in secret_values:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value
