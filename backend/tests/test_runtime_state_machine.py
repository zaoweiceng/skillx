from dataclasses import dataclass

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
