from app.application.security import REDACTED, event_payload, redact


def test_redactor_removes_nested_credentials() -> None:
    value = {
        "city": "成都",
        "api_key": "secret-value",
        "nested": {"access_token": "token-value", "result": "ok"},
    }

    assert redact(value) == {
        "city": "成都",
        "api_key": REDACTED,
        "nested": {"access_token": REDACTED, "result": "ok"},
    }


def test_event_payload_only_exposes_structured_output_summary() -> None:
    payload = {
        "runId": "run-1",
        "output": {"answer": "private value", "password": "hidden"},
    }

    assert event_payload(payload)["output"] == {
        "summary": "structured-object",
        "keys": ["answer", "password"],
    }


def test_redactor_honors_json_schema_sensitive_annotations() -> None:
    schema = {
        "type": "object",
        "properties": {
            "public": {"type": "string"},
            "session": {"type": "string", "writeOnly": True},
            "profile": {
                "type": "object",
                "x-skillx-sensitive": True,
            },
        },
    }

    assert redact(
        {
            "public": "visible",
            "session": "opaque-value",
            "profile": {"name": "private"},
        },
        schema,
    ) == {
        "public": "visible",
        "session": REDACTED,
        "profile": REDACTED,
    }
