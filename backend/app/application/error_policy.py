from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorPolicy:
    code: str
    retryable: bool
    max_attempts: int = 1
    backoff_seconds: float = 0
    evidence_kind: str = "unsupported"


_ALIASES = {
    "CONNECTION_ERROR": "DEPENDENCY_UNAVAILABLE",
    "CONNECTION_INTERRUPTED": "DEPENDENCY_UNAVAILABLE",
    "DNS_ERROR": "DEPENDENCY_UNAVAILABLE",
    "HTTP_503": "DEPENDENCY_UNAVAILABLE",
    "RATE_LIMIT": "RATE_LIMITED",
    "TOO_MANY_REQUESTS": "RATE_LIMITED",
    "TRANSPORT_ERROR": "DEPENDENCY_UNAVAILABLE",
}

_POLICIES = {
    "DEPENDENCY_UNAVAILABLE": ErrorPolicy(
        "DEPENDENCY_UNAVAILABLE", True, 2, 0.05, "transport_error"
    ),
    "TIMEOUT": ErrorPolicy("TIMEOUT", True, 2, 0.05, "timeout"),
    "RATE_LIMITED": ErrorPolicy("RATE_LIMITED", True, 3, 0.1, "transport_error"),
    # Agent/adapter 中断可进入 checkpoint，但不能在 Host 内盲目重启整个 Agent。
    "EXECUTION_FAILED": ErrorPolicy("EXECUTION_FAILED", True),
}


def error_policy(code: str) -> ErrorPolicy:
    canonical = _ALIASES.get(code, code)
    return _POLICIES.get(canonical, ErrorPolicy(canonical, False))


def normalize_error(
    code: str,
    message: str,
    *,
    retryable: bool | None = None,
) -> dict[str, object]:
    policy = error_policy(code)
    return {
        "code": policy.code,
        "message": message,
        "retryable": policy.retryable if retryable is None else retryable,
    }


def transport_error_code(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if (
        isinstance(exc, TimeoutError)
        or "timeout" in name
        or "timeout" in message
        or "超时" in message
    ):
        return "TIMEOUT"
    return "DEPENDENCY_UNAVAILABLE"
