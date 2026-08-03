from app.runtime.browser import (
    ObservedPage,
    PageContract,
    authorization_digest,
    detect_drift,
)
from app.runtime.contract_patch import (
    ContractPatchViolation,
    build_contract_patch,
)


def contract() -> PageContract:
    return PageContract.model_validate(
        {
            "contractVersion": "1",
            "urlPattern": "https://forms.example.org/apply*",
            "pageIdentity": {
                "identity": "user-1",
                "businessObject": "expense",
            },
            "fields": [
                {
                    "id": "name",
                    "required": True,
                    "type": "text",
                    "locator": {"role": "textbox", "label": "姓名"},
                }
            ],
            "submit": {"role": "button", "name": "提交申请"},
        }
    )


def test_required_field_added_is_structural_drift() -> None:
    observed = ObservedPage.model_validate(
        {
            "url": "https://forms.example.org/apply",
            "identity": "user-1",
            "businessObject": "expense",
            "fields": [
                {
                    "id": "name",
                    "required": True,
                    "type": "text",
                    "locator": {"role": "textbox", "label": "姓名"},
                },
                {
                    "id": "department",
                    "required": True,
                    "type": "text",
                    "locator": {"role": "textbox", "label": "部门"},
                },
            ],
            "submit": {"role": "button", "name": "提交申请"},
        }
    )

    result = detect_drift(contract(), observed)

    assert result.level == "L3"
    assert result.can_continue is False


def test_domain_change_is_security_drift() -> None:
    observed = ObservedPage(
        url="https://evil.example.net/apply",
        identity="user-1",
        businessObject="expense",
        fields=contract().fields,
        submit=contract().submit,
    )

    assert detect_drift(contract(), observed).level == "L4"


def test_same_domain_wrong_path_is_security_drift() -> None:
    observed = ObservedPage(
        url="https://forms.example.org/admin/delete",
        identity="user-1",
        businessObject="expense",
        fields=contract().fields,
        submit=contract().submit,
    )

    assert detect_drift(contract(), observed).level == "L4"


def test_identity_change_is_security_drift() -> None:
    observed = ObservedPage(
        url="https://forms.example.org/apply",
        identity="user-2",
        businessObject="expense",
        fields=contract().fields,
        submit=contract().submit,
    )

    assert detect_drift(contract(), observed).level == "L4"


def test_authorization_digest_changes_with_form_data() -> None:
    first = authorization_digest("run-1", "page-1", {"amount": 100}, "user-1", {"name": "提交"})
    second = authorization_digest("run-1", "page-1", {"amount": 101}, "user-1", {"name": "提交"})

    assert first != second


def _l3_observed_with_added_required() -> ObservedPage:
    return ObservedPage.model_validate(
        {
            "url": "https://forms.example.org/apply",
            "identity": "user-1",
            "businessObject": "expense",
            "fields": [
                {
                    "id": "name",
                    "required": True,
                    "type": "text",
                    "locator": {"role": "textbox", "label": "姓名"},
                },
                {
                    "id": "department",
                    "required": True,
                    "type": "text",
                    "locator": {"role": "textbox", "label": "部门"},
                },
            ],
            "submit": {"role": "button", "name": "提交申请"},
        }
    )


def test_contract_patch_proposes_fields_without_widening_scope() -> None:
    observed = _l3_observed_with_added_required()
    drift = detect_drift(contract(), observed)
    assert drift.level == "L3"

    patch = build_contract_patch(contract(), observed, drift.differences).to_dict()

    assert patch["status"] == "draft"
    assert patch["kind"] == "PageContractPatch"
    # 补丁只描述字段差异，基线 URL/身份保持不变（不在补丁内）。
    proposed_ids = {field["id"] for field in patch["proposedFields"]}
    assert proposed_ids == {"name", "department"}
    assert patch["differences"]


def test_contract_patch_rejects_identity_widening() -> None:
    observed = ObservedPage(
        url="https://forms.example.org/apply",
        identity="user-2",
        businessObject="expense",
        fields=contract().fields,
        submit=contract().submit,
    )
    try:
        build_contract_patch(contract(), observed, ["身份变化"])
    except ContractPatchViolation:
        return
    raise AssertionError("身份变化应被 ContractPatch 拒绝")
