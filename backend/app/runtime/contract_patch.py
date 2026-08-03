from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.runtime.browser import ObservedPage, PageContract


class ContractPatchViolation(ValueError):
    """ContractPatch 试图扩大 URL、身份、Tool allowlist 或权限范围。"""


@dataclass
class ContractPatch:
    """L3 漂移生成的 PageContract 补丁草稿。

    只描述差异，不修改当前 WorkflowVersion/PageContract。补丁不得扩大 URL 模式、
    身份、业务对象或权限范围；只允许在既有安全边界内新增/调整字段契约。人工审核并
    发布新版本后，才允许按新契约重放。
    """

    base_contract_version: str
    proposed_fields: list[dict[str, Any]]
    differences: list[str] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "PageContractPatch",
            "baseContractVersion": self.base_contract_version,
            "proposedFields": self.proposed_fields,
            "differences": self.differences,
            "diagnostics": self.diagnostics,
            "status": "draft",
        }


def build_contract_patch(
    contract: PageContract,
    observed: ObservedPage,
    differences: list[str],
) -> ContractPatch:
    """从 L3 观察结果生成受限的 ContractPatch 草稿。

    强约束：URL 模式、身份、业务对象不得变化（这些属于 L4 安全域）；补丁只允许调整
    字段契约。若观察页面的身份/业务对象/URL 与契约不一致，直接拒绝生成补丁。
    """

    if observed.security_warning:
        raise ContractPatchViolation("存在安全告警，禁止生成 ContractPatch")
    expected_identity = contract.page_identity.get("identity")
    expected_object = contract.page_identity.get("businessObject")
    if expected_identity is not None and observed.identity != expected_identity:
        raise ContractPatchViolation("身份变化不可通过 ContractPatch 扩大授权")
    if expected_object is not None and observed.business_object != expected_object:
        raise ContractPatchViolation("业务对象变化不可通过 ContractPatch 扩大授权")

    # 提交动作变化属于高风险，仅记录 diagnostic，不并入自动补丁字段。
    diagnostics: list[dict[str, str]] = []
    if contract.submit != observed.submit:
        diagnostics.append(
            {
                "level": "warning",
                "code": "SUBMIT_ACTION_CHANGED",
                "message": "提交动作发生变化，需人工确认是否纳入新版本",
            }
        )

    proposed_fields = [field.model_dump(by_alias=True) for field in observed.fields]
    return ContractPatch(
        base_contract_version=contract.contract_version,
        proposed_fields=proposed_fields,
        differences=list(differences),
        diagnostics=diagnostics,
    )
