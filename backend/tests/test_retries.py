from app.application.retries import auto_retry_allowed


def test_read_only_effect_always_retryable() -> None:
    assert auto_retry_allowed("none", idempotent=False) is True
    assert auto_retry_allowed("none", idempotent=True) is True


def test_write_effect_retryable_only_when_idempotent() -> None:
    assert auto_retry_allowed("write", idempotent=True) is True
    assert auto_retry_allowed("write", idempotent=False) is False


def test_destructive_effect_retryable_only_when_idempotent() -> None:
    assert auto_retry_allowed("destructive", idempotent=True) is True
    assert auto_retry_allowed("destructive", idempotent=False) is False


def test_unknown_outcome_never_retries() -> None:
    # 结果未知时无论 effect/idempotent 如何都不自动重试。
    assert auto_retry_allowed("none", idempotent=True, outcome="unknown") is False
    assert auto_retry_allowed("write", idempotent=True, outcome="unknown") is False
