"""Preserve the planner's external-operation obligations through compilation."""


def validate_required_effects(required, proposal):
    has_http = any(example.httpFixtures for example in proposal.examples)
    if not required and not has_http:
        return
    if proposal.kind != "direct" or proposal.executionProfile != "local-command":
        raise ValueError("requiredEffects 外部 HTTP 动作必须由 local-command 实际执行，不能降格为纯格式化；已有能力通过 Workflow 组合")
    validate_http_examples(required, proposal.examples)
    validate_http_failure_coverage(proposal.examples)


def validate_http_examples(required, examples, *, across_examples=False):
    covered = set()
    for example in examples:
        if getattr(example, "expectedError", None) or getattr(example, "failureOutput", None):
            continue
        effects = {"http.read" if item.method in {"GET", "HEAD", "OPTIONS"} else "http.write"
                   for item in example.httpFixtures}
        covered.update(effects)
        missing = set(required) - effects
        if missing and not across_examples:
            raise ValueError("HTTP 样例缺少 requiredEffects 对应实际请求 fixture: " + ", ".join(sorted(missing)))

    if across_examples and (missing := set(required) - covered):
        raise ValueError("HTTP 样例集缺少 requiredEffects 对应实际请求 fixture: " + ", ".join(sorted(missing)))


def validate_http_failure_coverage(examples):
    """Require demonstrated error semantics, not just a happy HTTP transcript."""
    failures = [example for example in examples if any(item.status >= 400 for item in example.httpFixtures)]
    if not failures:
        raise ValueError("真实 HTTP 能力必须提供至少一个 4xx/5xx 响应反例，证明不会把外部失败当业务成功")
    def semantic(value):
        if isinstance(value, dict):
            return any(semantic(item) for item in value.values())
        if isinstance(value, list):
            return any(semantic(item) for item in value)
        # HTTP status alone is transport evidence, not a business-failure signal.
        if value is False:
            return True
        if isinstance(value, str) and value.strip():
            return not (value.strip().isdigit() and 100 <= int(value.strip()) <= 599)
        return False
    for example in failures:
        if getattr(example, "expectedError", None):
            continue
        failure = getattr(example, "failureOutput", None)
        if not failure or not semantic(failure):
            raise ValueError("HTTP 失败反例必须声明 expectedError 或明确业务失败的 failureOutput；仅状态码不足以证明失败处理")
