from pathlib import Path

import pytest

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    ExpectedPlanContract,
)
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


class _ScriptedAdapter:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def events(self, _run_id: str, _request):
        for event in self._events:
            yield event

    async def cancel(self, _run_id: str) -> None:
        return None


def _replan_contract(plan: dict, states: dict[str, str]) -> ExpectedPlanContract:
    return ExpectedPlanContract(
        plan_snapshot=plan,
        plan_digest=digest_json(plan),
        revision=1,
        allowed_remaining_step_ids=("b",),
        source_checkpoint_id="checkpoint-1",
        step_states=states,
        step_attempts={"a": [], "b": []},
        step_state_digest=digest_json(states),
        original_input={"message": "test"},
        step_outputs={},
        artifact_outputs={},
        continuation_generation=1,
        max_revisions=1,
        used_revisions=0,
        remaining_revisions=1,
    )


async def test_host_replan_budget_exhaustion_rejects_second_distinct_patch(
    tmp_path: Path,
) -> None:
    states = {"a": "succeeded", "b": "remaining"}
    source = validate_agent_plan(
        _plan([_step("a"), _step("b", depends_on=["a"])]),
        allowed_capabilities={"lookup.table"},
        max_steps=2,
    )
    patch_one = {
        "schemaVersion": "1",
        "basePlanDigest": source.digest,
        "baseRevision": 1,
        "baseStepStateDigest": digest_json(states),
        "sourceCheckpointId": "checkpoint-1",
        "replaceStepIds": ["b"],
        "replacementSteps": [_step("c", depends_on=["a"])],
        "reason": "first",
    }
    revised = apply_replan_patch(
        source,
        patch_one,
        completed_step_ids={"a"},
        replaceable_step_ids={"b"},
        current_step_state_digest=digest_json(states),
        source_checkpoint_id="checkpoint-1",
        allowed_capabilities={"lookup.table"},
        max_steps=2,
    )
    revised_states = {
        "a": "succeeded",
        "b": "skipped",
        "c": "remaining",
    }
    patch_two = {
        "schemaVersion": "1",
        "basePlanDigest": revised.digest,
        "baseRevision": 2,
        "baseStepStateDigest": digest_json(revised_states),
        "sourceCheckpointId": "checkpoint-1",
        "replaceStepIds": ["c"],
        "replacementSteps": [_step("d", depends_on=["a"])],
        "reason": "second",
    }
    adapter = _ScriptedAdapter(
        [
            {"type": "agent.plan", "payload": source.canonical()},
            {"type": "agent.replan", "payload": patch_one},
            {"type": "agent.replan", "payload": patch_two},
        ]
    )

    with pytest.raises(AgentHostError) as exc_info:
        await AgentHost(adapter=adapter).execute(
            AgentHostRequest(
                run_id="budget-exhaustion",
                prompt="test",
                output_schema={"type": "object"},
                working_directory=tmp_path,
                requested_capabilities=("lookup.table",),
                expected_plan=_replan_contract(source.canonical(), states),
            )
        )
    assert exc_info.value.code == "REPLAN_BUDGET_EXHAUSTED"


async def test_host_idempotent_replan_replay_consumes_budget_once(
    tmp_path: Path,
) -> None:
    states = {"a": "succeeded", "b": "remaining"}
    source = validate_agent_plan(
        _plan([_step("a"), _step("b", depends_on=["a"])]),
        allowed_capabilities={"lookup.table"},
        max_steps=2,
    )
    patch = {
        "schemaVersion": "1",
        "basePlanDigest": source.digest,
        "baseRevision": 1,
        "baseStepStateDigest": digest_json(states),
        "sourceCheckpointId": "checkpoint-1",
        "replaceStepIds": ["b"],
        "replacementSteps": [_step("c", depends_on=["a"])],
        "reason": "repeat",
    }
    adapter = _ScriptedAdapter(
        [
            {"type": "agent.plan", "payload": source.canonical()},
            {"type": "agent.replan", "payload": patch},
            {"type": "agent.replan", "payload": patch},
            {
                "type": "agent.completed",
                "payload": {"output": {"reply": "done"}},
            },
        ]
    )

    result = await AgentHost(adapter=adapter).execute(
        AgentHostRequest(
            run_id="budget-idempotent",
            prompt="test",
            output_schema={
                "type": "object",
                "properties": {"reply": {"type": "string"}},
                "required": ["reply"],
            },
            working_directory=tmp_path,
            requested_capabilities=("lookup.table",),
            expected_plan=_replan_contract(source.canonical(), states),
        )
    )

    assert result.used_revisions == 1
    assert result.remaining_revisions == 0
    replans = [
        item for item in result.audit_events if item["type"] == "agent.replan"
    ]
    assert replans[-1]["payload"]["idempotentReplay"] is True
