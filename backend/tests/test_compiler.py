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


def test_compiler_rejects_output_field_not_in_upstream_schema() -> None:
    # 上游 LLM 节点声明 outputSchema 只有 category，下游引用不存在的 missing 字段。
    definition = deterministic_workflow()
    definition["spec"]["nodes"].insert(
        2,
        {
            "id": "classify",
            "type": "llm",
            "displayName": "分类",
            "input": {"text": "${format.output.text}"},
            "config": {
                "modelProfile": "fake",
                "outputSchema": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}},
                },
                "fakeOutput": {"category": "x"},
                "prompt": "{input}",
            },
        },
    )
    definition["spec"]["edges"] = [
        {"source": "input", "target": "format"},
        {"source": "format", "target": "classify"},
        {"source": "classify", "target": "output"},
    ]
    # output 引用 classify 不存在的字段 missing。
    output = next(node for node in definition["spec"]["nodes"] if node["id"] == "output")
    output["input"] = {"message": "${classify.output.missing}"}

    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)
    assert "OUTPUT_FIELD_NOT_IN_SCHEMA" in {
        item.code for item in error.value.diagnostics
    }


def test_compiler_allows_indexed_array_reference() -> None:
    # 统一表达式语法：${node.output.items[0]} 应被引用校验接受（不报未知引用）。
    definition = deterministic_workflow()
    definition["spec"]["nodes"][1]["config"] = {
        "operation": "passthrough",
    }
    output = next(node for node in definition["spec"]["nodes"] if node["id"] == "output")
    output["input"] = {"message": "${format.output.items[0]}"}
    compiled = WorkflowCompiler().compile(definition)
    # transform 无 outputSchema，不触发字段告警；引用语法被接受。
    assert "UNKNOWN_REFERENCE" not in {item.code for item in compiled.diagnostics}


def _fallback_workflow() -> dict:
    definition = deterministic_workflow()
    definition["spec"]["nodes"].append(
        {
            "id": "recovery_agent",
            "type": "agent",
            "displayName": "恢复 Agent",
            "input": {"message": "${input.name}"},
            "config": {
                "outputSchema": {"type": "object"},
                "maxSteps": 4,
                "allowedCapabilities": [],
            },
        }
    )
    fmt = next(node for node in definition["spec"]["nodes"] if node["id"] == "format")
    fmt["fallback"] = {"on": ["DEPENDENCY_UNAVAILABLE"], "target": "recovery_agent"}
    return definition


def test_compiler_accepts_valid_fallback() -> None:
    compiled = WorkflowCompiler().compile(_fallback_workflow())
    codes = {item.code for item in compiled.diagnostics}
    assert not codes & {
        "FALLBACK_TARGET_MISSING",
        "FALLBACK_TARGET_NOT_AGENT",
        "FALLBACK_UNSAFE_ERROR_CODE",
    }


def test_compiler_rejects_fallback_missing_target() -> None:
    definition = _fallback_workflow()
    fmt = next(node for node in definition["spec"]["nodes"] if node["id"] == "format")
    fmt["fallback"]["target"] = "ghost"
    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)
    assert "FALLBACK_TARGET_MISSING" in {item.code for item in error.value.diagnostics}


def test_compiler_rejects_unsafe_fallback_error_code() -> None:
    definition = _fallback_workflow()
    fmt = next(node for node in definition["spec"]["nodes"] if node["id"] == "format")
    fmt["fallback"]["on"] = ["PERMISSION_DENIED"]
    with pytest.raises(WorkflowValidationError) as error:
        WorkflowCompiler().compile(definition)
    assert "FALLBACK_UNSAFE_ERROR_CODE" in {item.code for item in error.value.diagnostics}
