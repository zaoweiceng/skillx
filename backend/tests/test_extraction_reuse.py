from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.application.extraction_reuse import validate_reuse_implementation
from app.application.learning_agents import (
    ComposedExtractionPlan,
    ExtractionPlan,
    validate_extraction,
)
from app.application.workflow_equivalence import find_equivalent_workflow, workflow_behavior_digest
from app.infrastructure.database import SessionLocal, Workflow
from app.infrastructure.models.tasks import LearningJob
from tests.test_learning_agents import context, fragment, pipeline
from tests.test_learning_batch import flow


def reuse_context():
    ctx = context()
    ctx["requireReuseReview"] = True
    ctx["catalog"]["capabilities"] = [
        {
            "id": "base",
            "name": "existing",
            "version": "1",
            "interfaces": {"execute": {"inputSchema": {"required": ["value"]}, "outputSchema": {}}},
        }
    ]
    ctx["existingSkills"] = [{"id": "guide", "name": "existing-guide", "content": "shared guide"}]
    return ctx


@pytest.mark.parametrize("group", ["skills", "skillx", "workflows"])
def test_new_artifact_requires_contract_comparison(group):
    plan = ExtractionPlan(
        decision="candidate", reason="fixture", **{group: [fragment("new", "flow-call")]}
    )
    with pytest.raises(ValueError, match="reuseReview"):
        validate_extraction(plan, reuse_context())
    part = getattr(plan, group)[0]
    from app.application.extraction_reuse import ReuseReview

    part.reuseReview = ReuseReview(comparedIds=["base"], gap="Adds a missing business rule")
    validate_extraction(plan, reuse_context())
    part.reuseReview.comparedIds = ["invented"]
    with pytest.raises(ValueError, match="目录之外"):
        validate_extraction(plan, reuse_context())


@pytest.mark.parametrize(
    "group,target",
    [
        ("skills", {"reuseSkillId": "guide"}),
        ("skillx", {"reuseVersionId": "base"}),
        ("workflows", {"reuseVersionId": "base"}),
    ],
)
async def test_direct_reuse_never_dispatches_builder_and_persists_result(
    monkeypatch, group, target
):
    from app.application.agent_host import AgentHost, AgentHostResult

    worker = await pipeline()
    requests = []

    async def choose(self, request, **kwargs):
        requests.append(request.output_schema["title"])
        assert requests == ["ExtractionPlan"]
        return AgentHostResult(
            output={
                "decision": "candidate",
                "reason": "Already covered",
                group: [fragment("reused", "flow-call", **target)],
            }
        )

    monkeypatch.setattr(AgentHost, "execute", choose)
    result = await worker.generate(reuse_context(), materializer=None)
    assert result.decision == "skip" and not result.failures
    async with SessionLocal() as session:
        job = await session.get(LearningJob, worker.job_id)
        assert job.result_json["reusedArtifacts"][group]["reused"] == next(iter(target.values()))
        assert await session.scalar(select(func.count()).select_from(Workflow)) == 0
    # Resume must not invoke the extractor or builders again.
    await worker.generate(reuse_context(), materializer=None)
    assert requests == ["ExtractionPlan"]


def test_reuse_rejects_unknown_or_wrong_kind():
    for target in ({"reuseVersionId": "missing"}, {"reuseSkillId": "guide"}):
        plan = ExtractionPlan(
            decision="candidate",
            reason="fixture",
            skillx=[fragment("reuse", "flow-call", **target)],
        )
        with pytest.raises(ValueError):
            validate_extraction(plan, reuse_context())


def test_extension_must_actually_call_declared_base():
    plan = ExtractionPlan(
        decision="candidate",
        reason="fixture",
        workflows=[
            fragment(
                "extension",
                "flow-call",
                reuseReview={
                    "comparedIds": ["base"],
                    "gap": "Additional validation",
                    "dependencyIds": ["base"],
                },
            )
        ],
    )
    part = plan.workflows[0]
    definition = flow("extension").definition
    with pytest.raises(ValueError, match="实际调用"):
        validate_reuse_implementation(part, definition, "workflows", reuse_context())
    definition["spec"]["nodes"][1]["type"] = "capability_call"
    definition["spec"]["nodes"][1]["config"]["capabilityVersionId"] = "base"
    validate_reuse_implementation(part, definition, "workflows", reuse_context())


def test_complete_composition_can_reuse_single_existing_workflow():
    plan = ComposedExtractionPlan(
        decision="candidate",
        reason="Whole process already covered",
        workflows=[fragment("existing-process", "flow-call", reuseVersionId="base")],
    )
    validate_extraction(plan, reuse_context())


def test_equivalence_ignores_names_and_labels_but_keeps_business_semantics():
    original = flow("original").definition
    renamed = deepcopy(original)
    renamed["metadata"]["name"] = "renamed"
    renamed["spec"]["inputSchema"]["description"] = "different prose"
    renamed["spec"]["nodes"][1]["displayName"] = "different label"
    renamed["spec"]["nodes"][1]["id"] = "renamed-node"
    for edge in renamed["spec"]["edges"]:
        for key in ("source", "target"):
            if edge[key] == "format":
                edge[key] = "renamed-node"
    for node in renamed["spec"]["nodes"]:
        node["input"] = {
            key: value.replace("${format.", "${renamed-node.") if isinstance(value, str) else value
            for key, value in node.get("input", {}).items()
        }
    assert workflow_behavior_digest(original) == workflow_behavior_digest(renamed)
    renamed["spec"]["nodes"][-1]["input"] = {"message": "different result"}
    assert workflow_behavior_digest(original) != workflow_behavior_digest(renamed)
    changed = deepcopy(original)
    changed["spec"]["inputSchema"]["properties"]["name"]["default"] = "Different"
    assert workflow_behavior_digest(original) != workflow_behavior_digest(changed)


async def test_archived_workflow_hidden_but_still_resolvable():
    from app.application.callable_capabilities import callable_catalog, resolve_callable
    from app.application.workflow_catalog import search_manifests
    from tests.test_workflow_catalog_plans import _published

    _, version, _ = await _published()
    async with SessionLocal() as session:
        owner = await session.get(Workflow, version.workflow_id)
        owner.draft_metadata_json = {"archived": True}
        await session.flush()
        assert not await callable_catalog(session)
        assert not await search_manifests(session, "问候")
        assert not await search_manifests(session, "")
        assert (await resolve_callable(session, version.id)).contract["id"] == version.id
        assert await find_equivalent_workflow(session, version.definition_json) is None


def test_model_schema_rejects_missing_review_and_invented_reuse_ids():
    from jsonschema import Draft202012Validator

    from app.application.extraction_reuse import bind_reuse_schema

    schema = bind_reuse_schema(ExtractionPlan.model_json_schema(), reuse_context())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    value = {"decision": "candidate", "reason": "test", "skills": [], "workflows": [],
             "skillx": [fragment("new", "flow-call")]}
    assert list(validator.iter_errors(value))
    value["skillx"][0]["reuseVersionId"] = "invented"
    assert list(validator.iter_errors(value))
    value["skillx"][0]["reuseVersionId"] = "base"
    assert list(validator.iter_errors(value))  # Effects must be decided explicitly.
    value["skillx"][0]["requiredEffects"] = []
    assert not list(validator.iter_errors(value))
    del value["workflows"]
    assert list(validator.iter_errors(value)), "Omitted composition must not silently become empty"


def test_skillx_extension_cannot_declare_unused_dependency():
    from types import SimpleNamespace

    from app.application.learning_agents import ReusableFragment

    part = ReusableFragment(
        **fragment(
            "extended",
            "flow-call",
            reuseReview={"comparedIds": ["base"], "gap": "increment", "dependencyIds": ["base"]},
        )
    )
    artifact = SimpleNamespace(
        dependencies={"base": {"kind": "skillx", "ref": "existing@1#execute"}},
        source="def execute(request, context): return request.input",
    )
    with pytest.raises(ValueError, match="精确版本依赖"):
        validate_reuse_implementation(part, artifact, "skillx", reuse_context())
    artifact.source = (
        "async def execute(request, context): "
        "return await context.capabilities.call('base', request.input)"
    )
    validate_reuse_implementation(part, artifact, "skillx", reuse_context())


async def test_reused_success_survives_an_unrelated_builder_failure():
    from app.application.learning_contracts import LearningProposal
    from app.application.learning_publishing import publish_artifacts

    worker = await pipeline()
    async with SessionLocal() as session, session.begin():
        job = await session.get(LearningJob, worker.job_id)
        job.result_json = {"reusedArtifacts": {"skillx": {"existing": "base"}}}
    status, output = await publish_artifacts(
        worker,
        LearningProposal(
            decision="candidate", reason="Partial reuse", failures={"skillx:new": "failed"}
        ),
        {},
    )
    assert status == "partial"
    assert output["reusedArtifacts"] == {"skillx": {"existing": "base"}}
    assert output["failures"] == {"skillx:new": "failed"}
