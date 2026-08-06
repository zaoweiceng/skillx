from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.application.compiler import digest_json


class AgentPlanError(ValueError):
    pass


class LiteralArgumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["literal"]
    value: Any


class UserInputArgumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["user_input"]
    inputKey: str = Field(default="message", min_length=1, max_length=120)
    extract: Literal["url"] | None = None


class StepOutputArgumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["step_output"]
    stepId: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)


class ArtifactArgumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["artifact"]
    artifactId: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)


ArgumentRef = Annotated[
    LiteralArgumentRef
    | UserInputArgumentRef
    | StepOutputArgumentRef
    | ArtifactArgumentRef,
    Field(discriminator="source"),
]
_ARGUMENT_REF_ADAPTER: TypeAdapter[ArgumentRef] = TypeAdapter(ArgumentRef)


class AgentPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=120)
    capabilityRef: str = Field(min_length=1, max_length=240)
    dependsOn: list[str] = Field(default_factory=list)
    argumentRefs: dict[str, ArgumentRef] = Field(default_factory=dict)
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
    baseStepStateDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sourceCheckpointId: str = Field(min_length=1, max_length=120)
    replaceStepIds: list[str] = Field(min_length=1)
    replacementSteps: list[AgentPlanStep] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


def parse_argument_ref(value: Any) -> ArgumentRef:
    try:
        return _ARGUMENT_REF_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise AgentPlanError(f"argumentRef 结构非法: {exc}") from exc


def _read_path(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if not segment:
            raise AgentPlanError(f"参数引用 path 非法: {path}")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise AgentPlanError(f"参数引用 path 不存在: {path}")
    return current


def materialize_step_arguments(
    step: AgentPlanStep,
    *,
    original_input: dict[str, Any],
    step_outputs: dict[str, dict[str, Any]],
    artifact_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for name, reference in step.argumentRefs.items():
        if isinstance(reference, LiteralArgumentRef):
            arguments[name] = reference.value
        elif isinstance(reference, UserInputArgumentRef):
            if reference.inputKey not in original_input:
                raise AgentPlanError(
                    f"计划步骤 {step.id} 缺少输入 {reference.inputKey}"
                )
            value = original_input[reference.inputKey]
            if reference.extract == "url":
                if not isinstance(value, str):
                    raise AgentPlanError(
                        f"计划步骤 {step.id} 的 URL 输入不是字符串"
                    )
                match = re.search(r"https?://[^\s\"}]+", value)
                if match is None:
                    raise AgentPlanError(
                        f"计划步骤 {step.id} 无法从输入解析 URL"
                    )
                value = match.group(0)
            arguments[name] = value
        elif isinstance(reference, StepOutputArgumentRef):
            source = step_outputs.get(reference.stepId)
            if source is None:
                raise AgentPlanError(
                    f"计划步骤 {step.id} 缺少前序输出 {reference.stepId}"
                )
            arguments[name] = _read_path(source, reference.path)
        elif isinstance(reference, ArtifactArgumentRef):
            source = artifact_outputs.get(reference.artifactId)
            if source is None:
                raise AgentPlanError(
                    f"计划步骤 {step.id} 缺少 Artifact {reference.artifactId}"
                )
            arguments[name] = _read_path(source, reference.path)
    return arguments


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
    replaceable_step_ids: set[str],
    current_step_state_digest: str,
    source_checkpoint_id: str,
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
    if (
        patch.baseStepStateDigest != current_step_state_digest
        or patch.sourceCheckpointId != source_checkpoint_id
    ):
        raise AgentPlanError("ReplanPatch checkpoint/step state 已过期")
    if plan.replanBoundary != "remaining_only":
        raise AgentPlanError("当前 PlanSnapshot 不允许 replan")
    replace_ids = set(patch.replaceStepIds)
    if len(replace_ids) != len(patch.replaceStepIds):
        raise AgentPlanError("ReplanPatch replaceStepIds 必须唯一")
    if not replace_ids <= replaceable_step_ids:
        raise AgentPlanError("ReplanPatch 只能替换 remaining closure")
    replacement_ids = {step.id for step in patch.replacementSteps}
    if replacement_ids & completed_step_ids:
        raise AgentPlanError("ReplanPatch 不得替换已完成步骤")
    completed_steps = [
        step.model_dump(mode="json")
        for step in plan.steps
        if step.id in completed_step_ids
    ]
    retained_steps = [
        step.model_dump(mode="json")
        for step in plan.steps
        if step.id not in replace_ids and step.id not in completed_step_ids
    ]
    candidate = {
        "schemaVersion": plan.schemaVersion,
        "planId": plan.planId,
        "revision": plan.revision + 1,
        "summary": patch.reason,
        "steps": [
            *completed_steps,
            *retained_steps,
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
