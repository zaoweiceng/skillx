import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routes import _binding_normalization, run_view, workflow_view
from app.application.agent_provider import (
    provider_readiness,
    reset_provider_probe_cache,
)
from app.application.error_policy import persisted_error, public_error, technical_error
from app.application.product_history import (
    bind_presentation_decision,
    normalize_presentation_policy,
    render_presentation_template,
    validate_presentation_decision,
    validate_presentation_template,
)
from app.application.security import sanitize_technical_detail
from app.config import get_settings
from app.infrastructure.database import Run, Workflow


def test_all_core_api_datetimes_use_utc_rfc3339() -> None:
    timestamp = datetime(2026, 8, 10, 7, 12, 23, tzinfo=UTC)
    workflow = Workflow(
        id=str(uuid4()),
        name="time-contract",
        draft_revision=1,
        draft_json={},
        created_at=timestamp,
        updated_at=timestamp,
    )
    run = Run(
        id=str(uuid4()),
        status="succeeded",
        input_json={},
        output_json={},
        created_at=timestamp,
        started_at=timestamp,
        finished_at=timestamp,
    )

    assert workflow_view(workflow)["createdAt"] == "2026-08-10T07:12:23Z"
    assert workflow_view(workflow)["updatedAt"] == "2026-08-10T07:12:23Z"
    assert run_view(run)["createdAt"] == "2026-08-10T07:12:23Z"
    assert run_view(run)["startedAt"] == "2026-08-10T07:12:23Z"
    assert run_view(run)["finishedAt"] == "2026-08-10T07:12:23Z"


def test_routes_do_not_return_orm_datetimes_directly() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "routes.py"
    ).read_text(encoding="utf-8")
    direct_datetime = re.compile(
        r'"[A-Za-z]+At"\s*:\s*'
        r"(?:item|run|checkpoint|conversation_run|message|evidence)\.[a-z_]+_at"
    )
    assert direct_datetime.search(source) is None


@pytest.mark.parametrize(
    ("value", "quote", "expected"),
    [
        ([20, 26], "2026", None),
        ([20, 26], "20,26", "delimited_array_v1"),
        ([20, 26], "[20, 26]", "json_array_v1"),
        (["上海", "北京"], "上海和北京", "delimited_array_v1"),
        (["上海", "北京"], "上海北京", None),
        (["北京", "上海"], "上海，北京", None),
        (["上海", "上海"], "上海", None),
        (["上海", "上海"], "上海，上海", "delimited_array_v1"),
    ],
)
def test_array_binding_requires_deterministic_reconstruction(
    value: list[object],
    quote: str,
    expected: str | None,
) -> None:
    assert _binding_normalization(value, quote) == expected


def test_presentation_template_is_schema_checked_and_rendered() -> None:
    schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }
    assert validate_presentation_template("问候：{message}", schema) == ("message",)
    assert render_presentation_template(
        "问候：{message} / {json}",
        {"message": "你好"},
    ).startswith("问候：你好 / ")

    with pytest.raises(ValueError, match="未知占位符"):
        normalize_presentation_policy(
            {
                "presentationPolicy": {
                    "enabled": True,
                    "audience": "end_user",
                    "producer": "template",
                    "template": "问候：{greeting}",
                }
            },
            execution_class="zero_llm",
            output_schema=schema,
        )
    with pytest.raises(ValueError, match="花括号无效"):
        validate_presentation_template("问候：{message", schema)
    with pytest.raises(ValueError, match="不能直接格式化对象或数组"):
        validate_presentation_template(
            "详情：{details}",
            {
                "type": "object",
                "properties": {"details": {"type": "object"}},
            },
        )


def test_presentation_decision_is_explicit_and_digest_bound() -> None:
    metadata = {
        "presentationPolicyProposal": {
            "enabled": True,
            "producer": "template",
            "template": "结果：{message}",
            "reason": "direct human result",
        },
        "presentationPolicyDecision": {"status": "pending"},
    }
    with pytest.raises(ValueError, match="尚未"):
        validate_presentation_decision(metadata)

    metadata["presentationPolicy"] = {
        "enabled": True,
        "audience": "end_user",
        "producer": "template",
        "template": "结果：{message}",
    }
    metadata["presentationPolicyDecision"] = {
        "status": "accepted",
        "reviewer": "local-user",
        "reviewedAt": "2026-08-11T10:00:00Z",
    }
    bind_presentation_decision(metadata)
    validate_presentation_decision(metadata)
    metadata["presentationPolicy"]["template"] = "已修改：{message}"
    with pytest.raises(ValueError, match="digest"):
        validate_presentation_decision(metadata)


def test_public_error_hides_and_technical_view_bounds_diagnostics() -> None:
    durable = persisted_error(
        {
            "code": "DEPENDENCY_UNAVAILABLE",
            "message": (
                "Browser launch /Users/local/project/chromium "
                "authorization: Bearer super-secret "
                "https://example.test?a=1&token=secret"
            ),
            "retryable": True,
        },
        technical_detail_ref="run:r1:node:n1",
    )

    projected = public_error(durable)
    assert projected is not None
    assert projected["message"] == "外部服务暂时不可用，请稍后重试。"
    assert "/Users/" not in str(projected)
    evidence = technical_error(durable)
    assert evidence is not None
    assert "[PATH]" in str(evidence["technicalDetail"])
    assert "super-secret" not in str(evidence)
    assert "token=secret" not in str(evidence)
    assert sanitize_technical_detail("x" * 5000).endswith("[TRUNCATED]")
    assert durable["errorEnvelopeVersion"] == 1
    assert persisted_error(durable) == durable
    assert public_error(persisted_error(durable)) == projected
    assert technical_error(persisted_error(durable)) == evidence


async def test_ready_reads_cached_facts_without_running_agent_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    reset_provider_probe_cache()
    monkeypatch.setattr(settings, "agent_provider_type", "real")
    monkeypatch.setattr(settings, "traex_command", ["python3"])
    monkeypatch.setattr(settings, "traex_allowed_executables", ["python3"])
    monkeypatch.setattr(settings, "model_endpoint", "https://provider.invalid/v1")
    monkeypatch.setattr(settings, "model_name", "model")
    monkeypatch.setattr(settings, "model_api_key_env", "TEST_PROVIDER_KEY")
    monkeypatch.setenv("TEST_PROVIDER_KEY", "configured")

    readiness = await provider_readiness()

    assert readiness["type"] == "real"
    assert readiness["modelAvailable"] is False
    assert readiness["lastProbe"]["kind"] == "not_run"
