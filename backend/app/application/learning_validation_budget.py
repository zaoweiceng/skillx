"""Apply an extraction job's remaining budget to every model call in validation."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
import json

from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.tasks import LearningJob


_scope = ContextVar("learning_validation_budget", default=None)


@contextmanager
def validation_budget(job_id, usage_sink):
    token = _scope.set((job_id, usage_sink))
    try:
        yield
    finally:
        _scope.reset(token)


async def _remaining(job_id):
    async with SessionLocal() as session:
        job = await session.get(LearningJob, job_id)
        if job is None:
            raise ValueError("提取验证任务不存在")
        budget = job.result_json.get("budget")
        if not budget:
            return None
        if job.result_json.get("validationUsageIncomplete"):
            raise ValueError("验证调用用量不完整，停止新的模型调用以避免超出本次提取预算")
        elapsed = (utcnow() - datetime.fromisoformat(budget["startedAt"]).replace(tzinfo=UTC)).total_seconds()
        return (int(budget["maxDurationSeconds"] - elapsed),
                budget["maxTokens"] - sum(job.result_json.get("usageByRequest", {}).values()))


async def _unknown_usage(job_id):
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, job_id)
        job.result_json = {**job.result_json, "validationUsageIncomplete": True}


def enforce_validation_budget(execute):
    """Scope is inherited by nested model SkillX; ordinary Agent runs are unchanged."""
    @wraps(execute)
    async def guarded(self, request, *, event_sink=None, cancellation=None):
        scope = _scope.get()
        if scope is None:
            return await execute(self, request, event_sink=event_sink, cancellation=cancellation)
        job_id, usage_sink = scope
        remaining = await _remaining(job_id)
        if remaining:
            seconds, tokens = remaining
            reserve = len((request.prompt + json.dumps(request.output_schema)).encode("utf-8")) + 4096
            output_limit = min(request.max_output_tokens or 32768, tokens - reserve)
            if seconds <= 0 or output_limit < 1:
                raise ValueError("本次提取预算不足，停止新的验证模型调用")
            request = replace(request, max_output_tokens=output_limit,
                              max_billable_tokens=min(request.max_billable_tokens or tokens, tokens),
                              max_total_tokens=min(request.max_total_tokens, tokens),
                              max_duration_seconds=min(request.max_duration_seconds, seconds))
        dispatched, completed = set(), set()

        async def sink(runtime_event, audit_event):
            payload = audit_event.get("payload", {})
            request_id = payload.get("requestId") or request.run_id
            if audit_event.get("type") == "model.request.dispatched":
                dispatched.add(request_id)
            elif audit_event.get("type") == "agent.step.completed":
                completed.add(request_id)
                metrics = {**payload, "totalTokens": payload.get("inputTokens", 0) + payload.get("outputTokens", 0)}
                await usage_sink(request_id, metrics)
                if not payload.get("usageComplete", False):
                    await _unknown_usage(job_id)
            if event_sink:
                await event_sink(runtime_event, audit_event)
        try:
            result = await execute(self, request, event_sink=sink, cancellation=cancellation)
            # Some adapters return a result without emitting per-request usage.
            if not completed:
                await usage_sink(request.run_id, {
                    "inputTokens": result.input_tokens, "outputTokens": result.output_tokens,
                    "totalTokens": result.total_tokens, "usageComplete": result.usage_complete})
                if not result.usage_complete:
                    await _unknown_usage(job_id)
                completed.update(dispatched)
            return result
        finally:
            if dispatched - completed:
                await _unknown_usage(job_id)
    return guarded
