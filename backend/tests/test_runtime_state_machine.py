from dataclasses import dataclass

import pytest

from app.runtime.state_machine import node_readiness, output_schema_errors


@dataclass
class Edge:
    source: str
    target: str
    when: bool | None = None


def test_condition_edges_select_only_matching_branch() -> None:
    incoming_true = [Edge("condition", "true_step", True)]
    incoming_false = [Edge("condition", "false_step", False)]
    statuses = {"condition": "succeeded"}
    outputs = {"condition": {"result": True}}
    on_errors = {"condition": "fail_run"}

    assert node_readiness(
        incoming_true, statuses, outputs, on_errors
    )[0] == "ready"
    assert node_readiness(
        incoming_false, statuses, outputs, on_errors
    )[0] == "skipped"


def test_on_error_continue_selects_downstream_edge() -> None:
    incoming = [Edge("best_effort", "output")]
    readiness, selected = node_readiness(
        incoming,
        {"best_effort": "failed"},
        {"best_effort": {"error": {"code": "EXPECTED"}}},
        {"best_effort": "continue"},
    )
    assert readiness == "ready"
    assert selected == [{"source": "best_effort", "target": "output"}]


def test_final_output_schema_is_shared_terminal_gate() -> None:
    schema = {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    }
    assert output_schema_errors(schema, {"message": "ok"}) == []
    assert output_schema_errors(schema, {"message": 1})


@pytest.mark.parametrize("needs_create", [True, False])
async def test_selected_branch_can_rejoin_without_reading_skipped_output(needs_create):
    from app.application.expressions import resolve_mapping
    from app.domain.workflow import WorkflowNode
    from app.runtime.executors import ExecutorRegistry

    nodes = [
        WorkflowNode(id="check", type="condition", displayName="Need create",
                     input={"value": needs_create, "expected": True}, config={"operation": "equals"}),
        WorkflowNode(id="create", type="transform", displayName="Created result",
                     input={"id": "new-id", "created": True}),
        WorkflowNode(id="reuse", type="transform", displayName="Existing result",
                     input={"id": "existing-id", "created": False}),
        WorkflowNode(id="output", type="output", displayName="Result", input={
            "record": {"$if": {"condition": "${check.output.result}",
                               "then": "${create.output}", "else": "${reuse.output}"}}}),
    ]
    edges = {"check": [], "create": [Edge("check", "create", True)],
             "reuse": [Edge("check", "reuse", False)],
             "output": [Edge("create", "output"), Edge("reuse", "output")]}
    states, outputs, executed = {}, {}, []
    for node in nodes:
        readiness, _ = node_readiness(edges[node.id], states, outputs, {})
        if readiness == "skipped":
            states[node.id] = "skipped"
            continue
        assert readiness == "ready"
        inputs = resolve_mapping(node.input, {}, outputs)
        result = await ExecutorRegistry().get(node.type).execute(None, node, inputs)
        assert result.status == "succeeded"
        states[node.id], outputs[node.id] = result.status, result.output
        executed.append(node.id)
    assert executed == ["check", "create" if needs_create else "reuse", "output"]
    assert outputs["output"] == {"record": {"id": "new-id" if needs_create else "existing-id",
                                             "created": needs_create}}
