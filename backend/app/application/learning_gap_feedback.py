"""One incremental planning pass for explicit, pre-publication graph gaps."""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.application.learning_agents import (
    ComposedExtractionPlan, ExtractionPlan, LearningAgentPipeline, LearningLeaseLost, ReusableFragment,
    WorkflowFragment, validate_extraction,
)
from app.application.learning_contracts import LearningProposal


class GapSkillX(ReusableFragment):
    requiredEffects: list[Literal["http.read", "http.write"]]


class GapWorkflow(WorkflowFragment):
    requiredEffects: list[Literal["http.read", "http.write"]]


class ExtractionGapDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1)
    addSkillX: list[GapSkillX] = Field(default_factory=list, max_length=8)
    addWorkflows: list[GapWorkflow] = Field(default_factory=list, max_length=8)
    # Values are additions, never replacements for the frozen dependencies.
    workflowDependencies: dict[str, list[str]]


def flows(plan):
    return [*plan.workflows, *([plan.workflow] if plan.workflow else [])]


def merge_gap_delta(plan, delta, gap_names, context):
    candidate = plan.model_copy(deep=True)
    original = [*plan.skills, *plan.skillx, *flows(plan)]
    original_names = {item.name for item in original}
    additions = [*delta.addSkillX, *delta.addWorkflows]
    new_names = {item.name for item in additions}
    if original_names & new_names or len(new_names) != len(additions):
        raise ValueError("缺口规划不能重建、改名覆盖已冻结片段")
    if set(delta.workflowDependencies) != set(gap_names):
        raise ValueError("缺口规划只能且必须为报告缺口的工作流补充依赖")
    touched = set()
    by_name = {item.name: item for item in flows(candidate)}
    for name, dependencies in delta.workflowDependencies.items():
        if not dependencies or not (set(dependencies) - set(by_name[name].dependsOn)):
            raise ValueError("缺口反馈必须补充实际依赖，不能原样重试工作流")
        by_name[name].dependsOn = list(dict.fromkeys([*by_name[name].dependsOn, *dependencies]))
        touched.update(dependencies)
    # New wrappers can depend on new atoms; every addition must be reachable
    # from an affected workflow, not an unrelated extraction opportunity.
    added_flows = {item.name: item for item in delta.addWorkflows}
    pending = list(touched)
    while pending:
        item = added_flows.get(pending.pop())
        if item:
            children = set(item.dependsOn) - touched
            touched.update(children)
            pending.extend(children)
    if not new_names <= touched:
        raise ValueError("新增片段必须成为缺口工作流的真实依赖")
    candidate.skillx.extend(delta.addSkillX)
    candidate.workflows.extend(delta.addWorkflows)
    # Revalidate field bounds as model_copy does not validate nested mutations.
    candidate = type(plan).model_validate(candidate.model_dump())
    validate_extraction(candidate, {**context, "requireReuseReview": True})
    return candidate


async def generate_with_gap_feedback(pipeline, context, *, materializer, publish=None):
    control_key = "extractor:gap-control"
    control = await pipeline._stage(control_key)
    if control.get("attempted"):
        if control.get("status") == "planned":
            result = await pipeline.generate(context, materializer=materializer, publish=publish,
                frozen_proposal=LearningProposal.model_validate(control["frozenProposal"]),
                frozen_names=set(control["frozenNames"]))
            await pipeline._record(control_key, {"status": "completed", "result": result.model_dump()})
            return result
        result = LearningProposal.model_validate(control.get("result") or control["originalProposal"])
        if control.get("status") != "completed":
            result.failures["planning:dependencies"] = control.get("error") or "缺口规划已消耗一次预算，未完成；不自动重新规划"
        return result  # The bound survives worker resumes and further gaps.
    proposal = await pipeline.generate(context, materializer=materializer, publish=publish)
    if context.get("kind") == "repair":
        return proposal
    saved = await pipeline._stage("extractor")
    if not saved.get("output"):
        return proposal
    model = ComposedExtractionPlan if context.get("requirements", {}).get("requireWorkflowComposition") else ExtractionPlan
    plan = model.model_validate(saved["output"])
    gaps = {}
    for fragment in flows(plan):
        graph = await pipeline._stage(pipeline.stage_key("workflow", fragment.name) + ":graph")
        output = graph.get("output") or {}
        if output.get("decision") == "needs_dependencies" and output.get("missingCapabilities"):
            gaps[fragment.name] = output["missingCapabilities"]
    if not gaps:
        return proposal
    # Consume before dispatch: even transport failures cannot silently start an
    # unbounded new planning budget on the next lease.
    await pipeline._record(control_key, {"attempted": True, "status": "running", "gaps": gaps,
        "originalProposal": proposal.model_dump()})
    try:
        data = {
            "goal": context.get("goal"),
            "originalAcceptance": LearningAgentPipeline.original_acceptance(context),
            "requirements": context.get("requirements", {}), "gaps": gaps,
            "frozenPlan": plan.model_dump(), "catalog": context.get("catalog", {}),
            "frozenContracts": [{"name": item.name, "inputSchema": item.inputSchema,
                "outputSchema": item.outputSchema} for item in proposal.skillx],
            "existingSkills": context.get("existingSkills", []),
            "calls": [{"id": item["id"], "status": item["status"], "kind": item.get("kind")}
                      for item in context["calls"]],
        }
        delta = await pipeline.call("extractor", ExtractionGapDelta, data, phase="gap",
            validate=lambda value: merge_gap_delta(plan, value, gaps, context))
        revised = merge_gap_delta(plan, delta, gaps, context)
        await pipeline._record("extractor", {"output": revised.model_dump()})
        retry = set(gaps)
        while True:
            parents = {item.name for item in flows(plan) if set(item.dependsOn) & retry}
            if parents <= retry:
                break
            retry.update(parents)
        frozen_names = {item.name for item in [*plan.skills, *plan.skillx, *flows(plan)]} - retry
        frozen = deepcopy(proposal)
        frozen.decision = "candidate"
        for name in retry:
            frozen.failures.pop("workflow:" + name, None)
        await pipeline._record(control_key, {"status": "planned", "frozenNames": sorted(frozen_names),
            "retryWorkflows": sorted(retry), "frozenProposal": frozen.model_dump()})
        result = await pipeline.generate(context, materializer=materializer, publish=publish,
            frozen_proposal=frozen, frozen_names=frozen_names)
        await pipeline._record(control_key, {"status": "completed", "result": result.model_dump()})
        return result
    except LearningLeaseLost:
        raise
    except Exception as exc:
        proposal.failures["planning:dependencies"] = str(exc)
        await pipeline._record(control_key, {"status": "failed", "error": str(exc)})
        return proposal
