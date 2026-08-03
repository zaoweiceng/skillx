from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class FieldContract(BaseModel):
    id: str
    required: bool = False
    type: str = "text"
    locator: dict[str, str] = Field(default_factory=dict)
    options: list[str] = Field(default_factory=list)


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
    fields: list[FieldContract]
    submit: dict[str, str] | None = None
    security_warning: bool = False


class DriftResult(BaseModel):
    level: Literal["L0", "L1", "L2", "L3", "L4"]
    differences: list[str] = Field(default_factory=list)
    can_continue: bool


def detect_drift(contract: PageContract, observed: ObservedPage) -> DriftResult:
    differences: list[str] = []
    expected_url = urlparse(contract.url_pattern.replace("*", ""))
    current_url = urlparse(observed.url)
    if observed.security_warning or (
        expected_url.hostname and current_url.hostname != expected_url.hostname
    ):
        return DriftResult(
            level="L4",
            differences=["目标域名或安全状态异常"],
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
