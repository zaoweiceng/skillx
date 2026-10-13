from types import SimpleNamespace

import pytest

from app.application.learning_contracts import ContractExample
from app.application.workflow_assertions import check_expected, verify_pure_output


def test_expected_projection_is_recursive_and_feedback_does_not_echo_values():
    check_expected({"status": "prepared", "nested": {"x": 1}},
                   {"status": "prepared", "nested": {"x": 1, "text": "free prose"}, "title": "variation"}, partial=True)
    with pytest.raises(ValueError, match=r"\$\.nested\.x") as caught:
        check_expected({"nested": {"x": "secret-expected"}}, {"nested": {"x": "secret-actual"}}, partial=True)
    assert "secret" not in str(caught.value)
    with pytest.raises(ValueError, match="unexpected field"):
        check_expected({"status": "prepared"}, {"status": "prepared", "title": "extra"})


async def test_pure_verify_checks_business_invariants_and_reports_assertion_line():
    example = ContractExample(input={"branch": "feature/demo"}, expected={"status": "prepared"},
        verify='assert result["status"] == expected["status"]\nassert inputs["branch"] in result["description"]\nassert error is None')
    await verify_pure_output(example, {"status": "prepared", "description": "Prepare feature/demo for review"}, example.input)
    with pytest.raises(ValueError, match="第 2 行失败：AssertionError"):
        await verify_pure_output(example, {"status": "prepared", "description": "secret actual output"}, example.input)


@pytest.mark.parametrize("operation", [
    "open(inputs['path']).read()", "open(inputs['path'], 'w').write('changed')",
    "__import__('subprocess').run(['echo', 'should not run'])",
    "__import__('socket').create_connection(('127.0.0.1', 8000))",
])
async def test_verify_uses_real_pure_boundary_for_io_and_effects(operation, tmp_path):
    protected = tmp_path / "protected.txt"
    protected.write_text("unchanged")
    example = ContractExample(input={"path": str(protected)}, expected={},
        verify="assert result is not None\n" + operation)
    with pytest.raises(ValueError, match="PermissionError"):
        await verify_pure_output(example, {}, example.input)
    assert protected.read_text() == "unchanged"


async def test_verify_rejects_placeholder_assertions():
    example = ContractExample(input={}, expected={}, verify="assert True")
    with pytest.raises(ValueError, match="业务 assert"):
        await verify_pure_output(example, {}, {})


async def test_local_verify_resolves_expected_fixture_and_checks_declared_fields():
    from app.application.local_validation import LocalValidation
    example = ContractExample(input={"branch": "feature/demo"},
        expected={"status": "prepared", "nested": {"sha": "${fixture.sha}"}},
        setup='import json\nfrom pathlib import Path\nPath(".skillx-fixture.json").write_text(json.dumps({"sha": "fixture-commit"}))',
        verify='assert result["status"] == expected["status"]\nassert expected["nested"]["sha"] == "fixture-commit"\nassert inputs["branch"] == "feature/demo"\nassert error is None')
    workspace = LocalValidation()
    try:
        await workspace.setup(example.setup)
        result = {"status": "prepared", "nested": {"sha": "fixture-commit", "extra": True}, "title": "variable prose"}
        await workspace.verify(example, result, example.input)
        result["nested"]["sha"] = "incorrect"
        with pytest.raises(ValueError, match=r"\$\.nested\.sha"):
            await workspace.verify(example, result, example.input)
        assert example.expected["nested"]["sha"] == "${fixture.sha}"
    finally:
        workspace.close()


async def test_workflow_nondeterministic_output_runs_verify_and_keeps_schema_validation(monkeypatch):
    from app.application import runtime_profiles
    from app.application.learning_validation import validate_candidate
    from app.infrastructure.database import SessionLocal
    from app.runtime.executors import LLMExecutor
    from app.domain.workflow import NodeResult

    async def profiles(*args):
        return {}
    monkeypatch.setattr(runtime_profiles, "workflow_profiles", profiles)
    response = {"status": "prepared", "description": "feature/demo is ready for review"}
    async def model(*args):
        return NodeResult(status="succeeded", output=response)
    monkeypatch.setattr(LLMExecutor, "execute", model)
    schema = {"type": "object", "required": ["status", "description"],
              "properties": {"status": {"type": "string"}, "description": {"type": "string"}}}
    graph = {"apiVersion": "workflow.platform/v1", "kind": "Workflow", "metadata": {"name": "handoff"},
        "spec": {"inputSchema": {"type": "object"}, "outputSchema": schema,
                 "nodes": [{"id": "input", "type": "input", "displayName": "input"},
                           {"id": "draft", "type": "llm", "displayName": "draft", "config": {"outputSchema": schema}},
                           {"id": "output", "type": "output", "displayName": "output", "input": {
                               "status": "${draft.output.status}", "description": "${draft.output.description}"}}],
                 "edges": [{"source": "input", "target": "draft"}, {"source": "draft", "target": "output"}]}}
    examples = [ContractExample(input={"branch": branch}, expected={"status": "prepared"},
        sourceInvocationId="call" if index == 0 else None, evidenceMode="derived",
        verify='assert result["status"] == "prepared"\nassert "review" in result["description"]')
        for index, branch in enumerate(["feature/demo", "feature/other"])]
    evidence = {"call": SimpleNamespace(status="succeeded", kind="code", output_json={"stdout": '{"ok":true}'})}
    async with SessionLocal() as session:
        await validate_candidate(session, graph, examples, evidence)
        response["description"] = "wrong handoff content"
        with pytest.raises(ValueError, match="AssertionError"):
            await validate_candidate(session, graph, examples, evidence)
        response["description"] = 123
        with pytest.raises(Exception, match="not of type 'string'"):
            await validate_candidate(session, graph, examples, evidence)
