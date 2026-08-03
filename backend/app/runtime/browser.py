from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatchcase
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field


class FieldContract(BaseModel):
    id: str
    required: bool = False
    type: str = "text"
    locator: dict[str, str] = Field(default_factory=dict)
    options: list[str] = Field(default_factory=list)
    sensitive: bool = False


class PageContract(BaseModel):
    contract_version: str = Field(default="1", alias="contractVersion")
    url_pattern: str = Field(alias="urlPattern")
    page_identity: dict[str, Any] = Field(default_factory=dict, alias="pageIdentity")
    fields: list[FieldContract]
    submit: dict[str, str] | None = None


class ObservedPage(BaseModel):
    url: str
    title: str = ""
    identity: str | None = None
    business_object: str | None = Field(default=None, alias="businessObject")
    fingerprint: str | None = None
    fields: list[FieldContract]
    submit: dict[str, str] | None = None
    security_warning: bool = False


class DriftResult(BaseModel):
    level: Literal["L0", "L1", "L2", "L3", "L4"]
    differences: list[str] = Field(default_factory=list)
    can_continue: bool


def detect_drift(contract: PageContract, observed: ObservedPage) -> DriftResult:
    differences: list[str] = []
    if observed.security_warning or not url_matches(contract.url_pattern, observed.url):
        return DriftResult(
            level="L4",
            differences=["目标 URL 或安全状态异常"],
            can_continue=False,
        )
    expected_identity = contract.page_identity.get("identity")
    expected_object = contract.page_identity.get("businessObject")
    if (
        expected_identity is not None
        and observed.identity != expected_identity
        or expected_object is not None
        and observed.business_object != expected_object
    ):
        return DriftResult(
            level="L4",
            differences=["登录身份或业务对象发生变化"],
            can_continue=False,
        )
    expected_title = contract.page_identity.get("title")
    if expected_title is not None and observed.title != expected_title:
        return DriftResult(
            level="L3",
            differences=["页面标题与契约不一致"],
            can_continue=False,
        )

    expected = {field.id: field for field in contract.fields}
    actual = {field.id: field for field in observed.fields}
    missing = expected.keys() - actual.keys()
    if missing:
        differences.append("原字段消失: " + ", ".join(sorted(missing)))
    added_required = [
        field.id for key, field in actual.items() if key not in expected and field.required
    ]
    if added_required:
        differences.append("新增必填字段: " + ", ".join(sorted(added_required)))
    changed = [
        key
        for key in expected.keys() & actual.keys()
        if expected[key].type != actual[key].type
        or expected[key].required != actual[key].required
        or (expected[key].options and expected[key].options != actual[key].options)
    ]
    if changed:
        differences.append("字段约束变化: " + ", ".join(sorted(changed)))
    if missing or added_required or changed or contract.submit != observed.submit:
        if contract.submit != observed.submit:
            differences.append("提交动作发生变化")
        return DriftResult(level="L3", differences=differences, can_continue=False)

    added_optional = [field.id for key, field in actual.items() if key not in expected]
    locator_changed = [
        key
        for key in expected.keys() & actual.keys()
        if expected[key].locator != actual[key].locator
    ]
    if added_optional or locator_changed:
        if added_optional:
            differences.append("新增可选字段: " + ", ".join(sorted(added_optional)))
        if locator_changed:
            differences.append("字段定位变化: " + ", ".join(sorted(locator_changed)))
        return DriftResult(level="L2", differences=differences, can_continue=False)

    if observed.url != contract.url_pattern and "*" in contract.url_pattern:
        return DriftResult(level="L1", differences=["URL 与通配模式匹配"], can_continue=True)
    return DriftResult(level="L0", can_continue=True)


def url_matches(pattern: str, actual: str) -> bool:
    expected = urlparse(pattern)
    current = urlparse(actual)
    if (
        expected.scheme.lower() != current.scheme.lower()
        or (expected.hostname or "").lower() != (current.hostname or "").lower()
        or expected.port != current.port
        or current.username
        or current.password
        or current.fragment
    ):
        return False
    expected_path = unquote(expected.path)
    current_path = unquote(current.path)
    if not fnmatchcase(current_path, expected_path):
        return False
    if expected.query and not fnmatchcase(current.query, expected.query):
        return False
    return True


def authorization_digest(
    run_id: str,
    page_fingerprint: str,
    form_values: dict[str, Any],
    identity: str | None,
    submit_action: dict[str, str] | None,
) -> str:
    payload = {
        "run_id": run_id,
        "page_fingerprint": page_fingerprint,
        "form_values": form_values,
        "identity": identity,
        "submit_action": submit_action,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def drift_authorization_digest(
    run_id: str,
    url: str,
    identity: str | None,
    business_object: str | None,
    page_fingerprint: str,
    form_values: dict[str, Any],
    drift_differences: list[str],
) -> str:
    """一次性漂移授权摘要。

    覆盖 URL、身份、业务对象、页面指纹、表单值和已确认的差异集合。任何一项在
    重放时发生变化都会导致摘要不同，从而使旧授权失效并要求重新确认。
    """

    payload = {
        "run_id": run_id,
        "url": url,
        "identity": identity,
        "business_object": business_object,
        "page_fingerprint": page_fingerprint,
        "form_values": form_values,
        "drift": sorted(drift_differences),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
