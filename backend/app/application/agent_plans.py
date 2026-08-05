from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.application.compiler import digest_json


class AgentPlanError(ValueError):
    pass


class AgentPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=120)
    capabilityRef: str = Field(min_length=1, max_length=240)
    dependsOn: list[str] = Field(default_factory=list)
    argumentRefs: dict[str, Any] = Field(default_factory=dict)
    effect: Literal["none", "read", "write", "unknown"] = "none"
    idempotencyKey: str | None = Field(default=None, max_length=240)
    outputName: str = Field(min_length=1, max_length=120)
    fallback: Literal["retry", "replan", "abort", "ask_user"] = "abort"


class AgentPlanSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal["1"] = "1"
    planId: str = Field(min_length=1, max_length=120)
    revision: int = Field(default=1, ge=1)
    summary: str = Field(default="", max_length=1000)
    steps: list[AgentPlanStep] = Field(min_length=1)
    replanBoundary: Literal["none", "remaining_only"] = "remaining_only"

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        return digest_json(self.canonical())


class ReplanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal["1"] = "1"
    basePlanDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    baseRevision: int = Field(ge=1)
    replacementSteps: list[AgentPlanStep] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


def validate_agent_plan(
    payload: dict[str, Any],
    *,
    allowed_capabilities: set[str],
    max_steps: int,
) -> AgentPlanSnapshot:
    try:
        plan = AgentPlanSnapshot.model_validate(payload)
    except ValueError as exc:
        raise AgentPlanError(f"agent.plan 结构非法: {exc}") from exc
    if len(plan.steps) > max_steps:
        raise AgentPlanError(f"计划步骤超过 Host 上限 {max_steps}")
    ids = [step.id for step in plan.steps]
    if len(ids) != len(set(ids)):
        raise AgentPlanError("agent.plan step id 必须唯一")
    known = set(ids)
    for step in plan.steps:
        if step.capabilityRef not in allowed_capabilities:
            raise AgentPlanError(
                f"计划步骤 {step.id} 使用未授权能力 {step.capabilityRef}"
            )
        if step.id in step.dependsOn or not set(step.dependsOn) <= known:
            raise AgentPlanError(f"计划步骤 {step.id} 依赖非法")
        if step.effect in {"write", "unknown"} and not step.idempotencyKey:
            raise AgentPlanError(
                f"计划步骤 {step.id} 的副作用缺少 idempotencyKey"
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {step.id: step.dependsOn for step in plan.steps}

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise AgentPlanError("agent.plan 依赖图存在环")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in dependencies[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)
    return plan


def apply_replan_patch(
    plan: AgentPlanSnapshot,
    payload: dict[str, Any],
    *,
    completed_step_ids: set[str],
    allowed_capabilities: set[str],
    max_steps: int,
) -> AgentPlanSnapshot:
    try:
        patch = ReplanPatch.model_validate(payload)
    except ValueError as exc:
        raise AgentPlanError(f"agent.replan 结构非法: {exc}") from exc
    if (
        patch.basePlanDigest != plan.digest
        or patch.baseRevision != plan.revision
    ):
        raise AgentPlanError("ReplanPatch base digest/revision 已过期")
    if plan.replanBoundary != "remaining_only":
        raise AgentPlanError("当前 PlanSnapshot 不允许 replan")
    replacement_ids = {step.id for step in patch.replacementSteps}
    if replacement_ids & completed_step_ids:
        raise AgentPlanError("ReplanPatch 不得替换已完成步骤")
    completed_steps = [
        step.model_dump(mode="json")
        for step in plan.steps
        if step.id in completed_step_ids
    ]
    candidate = {
        "schemaVersion": plan.schemaVersion,
        "planId": plan.planId,
        "revision": plan.revision + 1,
        "summary": patch.reason,
        "steps": [
            *completed_steps,
            *[
                step.model_dump(mode="json")
                for step in patch.replacementSteps
            ],
        ],
        "replanBoundary": plan.replanBoundary,
    }
    return validate_agent_plan(
        candidate,
        allowed_capabilities=allowed_capabilities,
        max_steps=max_steps,
    )
