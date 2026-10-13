"""Zero-call eligibility gate; explicit extraction and failure repair stay independent."""
from pydantic import ValidationError

from app.application.task_contracts import LearningCandidate


def automatic_learning_decision(state: dict, calls: list) -> tuple[dict | None, str]:
    candidate = state.get("learningCandidate")
    if not candidate:
        return None, "没有经执行验证的新增方法；普通问答与常规复用不启动自动提炼"
    try:
        nomination = LearningCandidate.model_validate(candidate)
    except ValidationError:
        return None, "学习提名不符合契约"
    successful = {call.id: call for call in calls if call.status == "succeeded"}
    if not set(nomination.sourceInvocationIds).issubset(successful):
        return None, "学习提名缺少成功执行证据"
    selected = [successful[key] for key in nomination.sourceInvocationIds]
    if len(selected) == 1 and selected[0].kind in {"workflow", "skillx"}:
        return None, "单个已发布能力的成功复用不产生新能力"
    return nomination.model_dump(), "已提名经成功执行验证的新增方法或修复经验"
