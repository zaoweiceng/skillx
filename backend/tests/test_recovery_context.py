from app.application.recovery_context import relevant_checkpoint_values


def test_context_excludes_unrelated_completed_values_and_inputs():
    snapshot = {
        "completedOutputs": {"needed": {"value": 3}, "unrelated": {"private": "secret"}},
        "runInput": {"region": "east", "private": "secret"},
        "remainingNodes": [{"id": "next", "input": {
            "value": "${needed.output.value}", "region": "${input.region}",
        }}],
        "remainingEdges": [{"source": "needed", "target": "next"}],
    }
    outputs, inputs = relevant_checkpoint_values(snapshot)
    assert outputs == {"needed": {"value": 3}}
    assert inputs == {"region": "east"}
    assert snapshot["completedOutputs"]["unrelated"] == {"private": "secret"}
