import pytest

from app.application.compiler import WorkflowCompiler, WorkflowValidationError
from tests.fixtures import deterministic_workflow


def test_compiler_builds_topological_order() -> None:
    compiled = WorkflowCompiler().compile(deterministic_workflow())

    assert compiled.topological_order == ["input", "format", "output"]
    assert compiled.digest.startswith("sha256:")


def test_compiler_rejects_cycles() -> None:
    definition = deterministic_workflow()
    definition["spec"]["edges"].append({"source": "output", "target": "format"})

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)

    assert "CYCLIC_GRAPH" in {item.code for item in error.value.diagnostics}


def test_compiler_rejects_unsafe_side_effect_retry() -> None:
    definition = deterministic_workflow(with_approval=True)
    approval = next(node for node in definition["spec"]["nodes"] if node["id"] == "approval")
    approval["retry"] = {"maxAttempts": 2}

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)

    assert "UNSAFE_SIDE_EFFECT_RETRY" in {item.code for item in error.value.diagnostics}
