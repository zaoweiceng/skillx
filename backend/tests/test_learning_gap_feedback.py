from copy import deepcopy
import pytest
from app.application.learning_agents import ExtractionPlan
from app.application.learning_contracts import LearningProposal
from app.application.learning_gap_feedback import ExtractionGapDelta, generate_with_gap_feedback, merge_gap_delta


def frag(name, **extra):
    return dict(name=name, description=name, scope="reusable", useWhen=["needed"], sourceInvocationIds=["source"], requiredEffects=[], **extra)


def context():
    return {"kind": "distill", "goal": "match every field across every candidate", "calls": [{"id": "source", "status": "succeeded"}], "catalog": {}, "existingSkills": []}


def plan():
    return ExtractionPlan(decision="candidate", reason="initial", skillx=[frag("read")], workflows=[frag("compose", dependsOn=["read"]), frag("parent", dependsOn=["compose"]), frag("unrelated")])


def delta(**change):
    return ExtractionGapDelta.model_validate(dict(reason="missing comparison", addSkillX=[frag("select")], workflowDependencies={"compose": ["select"]}) | change)


def test_delta_preserves_originals():
    original = plan()
    result = merge_gap_delta(original, delta(), {"compose"}, context())
    assert result.skillx[0] == original.skillx[0]
    assert result.workflows[0].model_dump(exclude={"dependsOn"}) == original.workflows[0].model_dump(exclude={"dependsOn"})
    assert result.workflows[0].dependsOn == ["read", "select"]
    assert original.workflows[0].dependsOn == ["read"]


@pytest.mark.parametrize("change", [
    {"addSkillX": [frag("read")]}, {"workflowDependencies": {"unrelated": ["select"]}},
    {"workflowDependencies": {"compose": ["read"]}}, {"addSkillX": [frag("select"), frag("extra")]},
    {"workflowDependencies": {"compose": ["unknown"]}},
])
def test_reject_overwrite_unrelated_noop_or_unbound(change):
    with pytest.raises(ValueError):
        merge_gap_delta(plan(), delta(**change), {"compose"}, context())


class Pipeline:
    stage_key = staticmethod(lambda role, name="": role + (":" + name if name else ""))
    def __init__(self, fail=False):
        self.stages = {"extractor": {"output": plan().model_dump()}, "workflow:compose:graph": {"output": {"decision": "needs_dependencies", "missingCapabilities": [{"name": "select", "reason": "inspect all"}]}}}
        self.generated, self.planning, self.fail = [], 0, fail
    async def _stage(self, key):
        return deepcopy(self.stages.get(key, {}))
    async def _record(self, key, changes):
        self.stages[key] = {**self.stages.get(key, {}), **deepcopy(changes)}
    async def generate(self, context, **kwargs):
        self.generated.append(kwargs)
        return kwargs.get("frozen_proposal") or LearningProposal(decision="candidate", reason="initial", failures={"workflow:compose": "gap", "workflow:parent": "dependency", "skillx:failed": "keep"})
    async def call(self, role, schema, data, **kwargs):
        self.planning += 1
        assert data["goal"] == context()["goal"]
        if self.fail:
            raise ValueError("model failed")
        value = delta()
        kwargs["validate"](value)
        return value


async def test_once_freezes_originals_and_retries_ancestors():
    pipe = Pipeline()
    result = await generate_with_gap_feedback(pipe, context(), materializer=None)
    assert pipe.planning == 1 and len(pipe.generated) == 2
    assert pipe.generated[1]["frozen_names"] == {"read", "unrelated"}
    assert result.failures == {"skillx:failed": "keep"}
    await generate_with_gap_feedback(pipe, context(), materializer=None)
    assert pipe.planning == 1 and len(pipe.generated) == 2


async def test_failed_dispatch_consumes_budget_across_resume():
    pipe = Pipeline(fail=True)
    for _ in range(2):
        result = await generate_with_gap_feedback(pipe, context(), materializer=None)
        assert result.failures["planning:dependencies"] == "model failed"
    assert pipe.planning == 1 and len(pipe.generated) == 1


async def test_resume_planned_keeps_frozen_snapshot():
    pipe = Pipeline()
    await generate_with_gap_feedback(pipe, context(), materializer=None)
    pipe.stages["extractor:gap-control"]["status"] = "planned"
    await generate_with_gap_feedback(pipe, context(), materializer=None)
    assert pipe.planning == 1 and len(pipe.generated) == 3
    assert pipe.generated[-1]["frozen_names"] == {"read", "unrelated"}


async def test_actual_graph_cache_invalidates_when_gap_dependency_added(monkeypatch):
    from tests.test_learning_agents import pipeline, mock_agents, fragment
    from app.application.learning_agents import WorkflowFragment
    worker = await pipeline()
    gap = {"decision": "needs_dependencies", "reason": "Need selector", "missingCapabilities": [{"name": "select", "description": "Select all", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}, "reason": "No selector"}]}
    values, requests, order = {"WorkflowGraphDraft": gap}, [], []
    mock_agents(monkeypatch, values, requests, order)
    part = WorkflowFragment(**fragment("compose", "source", requiredEffects=["http.read"]))
    inputs = {"fragment": part.model_dump()}
    for _ in range(2):
        with pytest.raises(ValueError, match="工作流缺少必要依赖"):
            await worker.build_workflow(part, inputs, lambda x: None)
    assert len(requests) == 1  # same input reuses the cached diagnostic
    part.dependsOn.append("select")
    inputs["fragment"] = part.model_dump()
    with pytest.raises(ValueError, match="工作流缺少必要依赖"):
        await worker.build_workflow(part, inputs, lambda x: None)
    assert len(requests) == 2  # a changed dependency contract invokes the builder


async def test_actual_generate_skips_frozen_build_and_publish(monkeypatch):
    from tests.test_learning_agents import pipeline, context as source_context, responses
    from app.application.learning_agents import ExtractionPlan
    worker = await pipeline()
    initial = ExtractionPlan.model_validate(responses()["ExtractionPlan"])
    await worker._record("extractor", {"status": "completed", "output": initial.model_dump()})
    async def forbidden(*args, **kwargs):
        pytest.fail("frozen stage must not build or publish")
    monkeypatch.setattr(worker, "call", forbidden)
    monkeypatch.setattr(worker, "build_skillx", forbidden)
    monkeypatch.setattr(worker, "build_workflow", forbidden)
    frozen = LearningProposal(decision="candidate", reason="completed", failures={"keep": "diagnostic"})
    result = await worker.generate(source_context(), materializer=forbidden, publish=forbidden,
        frozen_proposal=frozen, frozen_names={"guide", "calculate", "fixed-flow"})
    assert result.failures == frozen.failures
