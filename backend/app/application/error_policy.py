from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.application.security import bounded_technical_json


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
    "MODEL_AUTH_FAILED": ErrorPolicy("MODEL_AUTH_FAILED", False),
    "MODEL_CAPABILITY_UNSUPPORTED": ErrorPolicy(
        "MODEL_CAPABILITY_UNSUPPORTED", False
    ),
    "MODEL_MALFORMED_OUTPUT": ErrorPolicy("MODEL_MALFORMED_OUTPUT", False),
    "MODEL_PROVIDER_ERROR": ErrorPolicy("MODEL_PROVIDER_ERROR", False),
    "TRAE_NOT_INSTALLED": ErrorPolicy("TRAE_NOT_INSTALLED", False),
    "TRAE_AUTH_REQUIRED": ErrorPolicy("TRAE_AUTH_REQUIRED", False),
    "TRAE_MODEL_UNAVAILABLE": ErrorPolicy("TRAE_MODEL_UNAVAILABLE", False),
    "TRAE_WORKDIR_UNTRUSTED": ErrorPolicy("TRAE_WORKDIR_UNTRUSTED", False),
    "AGENT_PROTOCOL_INCOMPATIBLE": ErrorPolicy(
        "AGENT_PROTOCOL_INCOMPATIBLE", False
    ),
    # Agent/adapter 中断可进入 checkpoint，但不能在 Host 内盲目重启整个 Agent。
    "EXECUTION_FAILED": ErrorPolicy("EXECUTION_FAILED", True),
}

_USER_ERRORS: dict[str, tuple[str, str]] = {
    "DEPENDENCY_UNAVAILABLE": (
        "外部服务暂时不可用，请稍后重试。",
        "retry",
    ),
    "TIMEOUT": ("执行超时，请重试或缩小任务范围。", "retry"),
    "RATE_LIMITED": ("服务请求过于频繁，请稍后重试。", "retry_later"),
    "MODEL_NOT_CONFIGURED": ("真实模型尚未配置，请在系统设置中完成配置。", "configure"),
    "MODEL_RUNTIME_UNAVAILABLE": (
        "Agent 不可用，请在系统设置中配置 Pi 或其他 Agent。",
        "configure",
    ),
    "MODEL_RUNTIME_CONFIG_DRIFT": (
        "模型 Runtime 配置在运行期间发生变化，请重新发起运行。",
        "retry",
    ),
    "MODEL_AUTH_FAILED": ("模型凭据无效或无权访问当前模型。", "configure"),
    "MODEL_CAPABILITY_UNSUPPORTED": (
        "当前模型不支持所需的 function calling 或严格结构化输出。",
        "configure",
    ),
    "MODEL_MALFORMED_OUTPUT": ("模型返回了无法解析的结构化结果。", "view_run"),
    "MODEL_PROVIDER_ERROR": ("模型服务拒绝了本次请求，请查看兼容性诊断。", "view_run"),
    "TRAE_NOT_INSTALLED": ("未安装 TraeX，请先安装后重试。", "configure"),
    "TRAE_AUTH_REQUIRED": (
        "TraeX 登录已失效，请在终端运行 traex login 后重试。",
        "configure",
    ),
    "TRAE_MODEL_UNAVAILABLE": ("TraeX 当前模型不可用，请切换模型后重试。", "configure"),
    "TRAE_WORKDIR_UNTRUSTED": (
        "内置 Agent 工作目录未通过平台隔离校验，请重试运行；若持续出现请联系维护者。",
        "view_run",
    ),
    "AGENT_PROTOCOL_INCOMPATIBLE": (
        "当前 TraeX 版本与内置 bridge 不兼容，请升级后重试。",
        "configure",
    ),
    "PERMISSION_DENIED": ("当前任务需要未授权的能力。", "review_permissions"),
    "CONTRACT_VIOLATION": ("执行结果不符合已发布契约，请查看运行。", "view_run"),
    "INVALID_INPUT": ("输入不符合组件要求，请检查后重试。", "edit_input"),
    "PRESENTATION_FAILED": ("阅读结果生成失败，结构化结果不受影响。", "view_run"),
    "CANCELLED": ("运行已取消。", "retry"),
    "UNKNOWN_OUTCOME": ("无法确认操作是否完成，请先查看运行再决定是否重试。", "view_run"),
    "EXECUTION_FAILED": ("运行未能完成，请查看运行详情。", "view_run"),
    "EXECUTOR_INTERNAL_ERROR": ("执行器发生内部错误，请查看运行并重试。", "view_run"),
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


def persisted_error(
    error: dict[str, Any] | None,
    *,
    technical_detail_ref: str | None = None,
) -> dict[str, Any]:
    """Build the durable error envelope shared by every runtime."""

    raw = dict(error or {})
    if raw.get("errorEnvelopeVersion") == 1:
        envelope = {
            **raw,
            "technicalDetail": bounded_technical_json(
                raw.get("technicalDetail", {})
            ),
        }
        if technical_detail_ref is not None:
            envelope["technicalDetailRef"] = technical_detail_ref
        return envelope
    code = error_policy(str(raw.get("code", "EXECUTION_FAILED"))).code
    user_message, action = _USER_ERRORS.get(
        code,
        ("运行未能完成，请查看运行详情。", "view_run"),
    )
    retryable = bool(raw.get("retryable", error_policy(code).retryable))
    technical = raw.get("technicalDetail")
    if technical is None:
        technical = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "code",
                "userMessage",
                "technicalDetailRef",
                "retryable",
                "action",
                "taskCheckpointId",
                "checkpointId",
            }
        }
    envelope = {
        "errorEnvelopeVersion": 1,
        "code": code,
        "userMessage": str(raw.get("userMessage") or user_message),
        "technicalDetailRef": technical_detail_ref or raw.get("technicalDetailRef"),
        "retryable": retryable,
        "action": str(raw.get("action") or action),
        "technicalDetail": bounded_technical_json(technical),
    }
    for key, value in raw.items():
        if key not in {
            "code",
            "message",
            "userMessage",
            "technicalDetailRef",
            "retryable",
            "action",
            "technicalDetail",
        }:
            envelope[key] = bounded_technical_json(value)
    for key in ("taskCheckpointId", "checkpointId"):
        if isinstance(raw.get(key), str):
            envelope[key] = raw[key]
    return envelope


def public_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a durable error to the default user-safe API contract."""

    if error is None:
        return None
    envelope = persisted_error(error)
    result = {
        "code": envelope["code"],
        "userMessage": envelope["userMessage"],
        # Compatibility for existing clients. This is intentionally the safe text.
        "message": envelope["userMessage"],
        "technicalDetailRef": envelope["technicalDetailRef"],
        "retryable": envelope["retryable"],
        "action": envelope["action"],
    }
    for key in ("taskCheckpointId", "checkpointId"):
        if isinstance(envelope.get(key), str):
            result[key] = envelope[key]
    return result


def technical_error(error: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return bounded technical evidence only from an explicit detail endpoint."""

    if error is None:
        return None
    envelope = persisted_error(error)
    return {
        **public_error(envelope),
        "technicalDetail": envelope["technicalDetail"],
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
