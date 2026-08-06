import pytest

from app.application.agent_plans import (
    AgentPlanError,
    apply_replan_patch,
    validate_agent_plan,
)
from app.application.compiler import digest_json


def _step(
    step_id: str,
    capability: str = "lookup.table",
    *,
    depends_on: list[str] | None = None,
    effect: str = "none",
    idempotency_key: str | None = None,
) -> dict:
    return {
        "id": step_id,
        "action": "transform",
        "capabilityRef": capability,
        "dependsOn": depends_on or [],
        "argumentRefs": {},
        "effect": effect,
        "idempotencyKey": idempotency_key,
        "outputName": f"{step_id}Output",
        "fallback": "retry",
    }


def _plan(steps: list[dict]) -> dict:
    return {
        "schemaVersion": "1",
        "planId": "plan-1",
        "revision": 1,
        "summary": "test",
        "steps": steps,
        "replanBoundary": "remaining_only",
    }


def test_agent_plan_accepts_bounded_dag() -> None:
    plan = validate_agent_plan(
        _plan([_step("a"), _step("b", depends_on=["a"])]),
        allowed_capabilities={"lookup.table"},
        max_steps=2,
    )
    assert plan.digest.startswith("sha256:")


@pytest.mark.parametrize(
    "steps",
    [
        [_step("a"), _step("a")],
        [_step("a", depends_on=["missing"])],
        [_step("a", depends_on=["b"]), _step("b", depends_on=["a"])],
        [_step("a", capability="forbidden.tool")],
        [_step("a", effect="write")],
    ],
)
def test_agent_plan_fails_closed_for_invalid_graph_or_boundary(
    steps: list[dict],
) -> None:
    with pytest.raises(AgentPlanError):
        validate_agent_plan(
            _plan(steps),
            allowed_capabilities={"lookup.table"},
            max_steps=2,
        )


def test_replan_patch_replaces_only_remaining_steps_and_increments_revision() -> None:
    plan = validate_agent_plan(
        _plan([_step("a"), _step("b", depends_on=["a"])]),
        allowed_capabilities={"lookup.table"},
        max_steps=3,
    )
    replacement = _step("c", depends_on=["a"])
    states = {"a": "succeeded", "b": "remaining"}
    revised = apply_replan_patch(
        plan,
        {
            "schemaVersion": "1",
            "basePlanDigest": plan.digest,
            "baseRevision": 1,
            "baseStepStateDigest": digest_json(states),
            "sourceCheckpointId": "checkpoint-1",
            "replaceStepIds": ["b"],
            "replacementSteps": [replacement],
            "reason": "替换未执行步骤",
        },
        completed_step_ids={"a"},
        replaceable_step_ids={"b"},
        current_step_state_digest=digest_json(states),
        source_checkpoint_id="checkpoint-1",
        allowed_capabilities={"lookup.table"},
        max_steps=3,
    )
    assert revised.revision == 2
    assert [step.id for step in revised.steps] == ["a", "c"]
    assert revised.digest != plan.digest


def test_replan_patch_rejects_completed_step_replacement() -> None:
    plan = validate_agent_plan(
        _plan([_step("a"), _step("b", depends_on=["a"])]),
        allowed_capabilities={"lookup.table"},
        max_steps=2,
    )
    with pytest.raises(AgentPlanError, match="已完成"):
        states = {"a": "succeeded", "b": "remaining"}
        apply_replan_patch(
            plan,
            {
                "schemaVersion": "1",
                "basePlanDigest": plan.digest,
                "baseRevision": 1,
                "baseStepStateDigest": digest_json(states),
                "sourceCheckpointId": "checkpoint-1",
                "replaceStepIds": ["b"],
                "replacementSteps": [_step("a")],
                "reason": "非法替换",
            },
            completed_step_ids={"a"},
            replaceable_step_ids={"b"},
            current_step_state_digest=digest_json(states),
            source_checkpoint_id="checkpoint-1",
            allowed_capabilities={"lookup.table"},
            max_steps=2,
        )
