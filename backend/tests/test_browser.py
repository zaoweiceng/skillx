from app.runtime.browser import (
    ObservedPage,
    PageContract,
    authorization_digest,
    detect_drift,
)


def contract() -> PageContract:
    return PageContract.model_validate(
        {
            "contractVersion": "1",
            "urlPattern": "https://forms.example.org/apply*",
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
        fields=contract().fields,
        submit=contract().submit,
    )

    assert detect_drift(contract(), observed).level == "L4"


def test_authorization_digest_changes_with_form_data() -> None:
    first = authorization_digest("run-1", "page-1", {"amount": 100}, "user-1", {"name": "提交"})
    second = authorization_digest("run-1", "page-1", {"amount": 101}, "user-1", {"name": "提交"})

    assert first != second
