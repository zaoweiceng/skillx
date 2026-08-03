from __future__ import annotations

# 004-C / P2-4：唯一重试策略。发布校验、MCP Tool test 与 Orchestrator 共用，
# 避免“测试页说不可重试、运行时却重试”的审计不一致。
#
# 规则：只有只读（effect == "none"）或幂等的能力才允许自动重试；有副作用且非幂等的
# 能力禁止自动重试。结果未知（UNKNOWN_OUTCOME）时无论如何都不重试，交由人工确认。


def auto_retry_allowed(effect: str, idempotent: bool, *, outcome: str | None = None) -> bool:
    """给定 effect/idempotent（及可选结果），判断是否允许自动重试。

    - ``outcome == "unknown"``：副作用是否已发生不可知，禁止重试；
    - ``effect == "none"``：只读，安全重试；
    - 其它：仅当声明幂等时允许重试。
    """

    if outcome == "unknown":
        return False
    if effect == "none":
        return True
    return bool(idempotent)
