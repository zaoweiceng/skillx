import pytest
from app.application.agent_host import AgentHost, AgentHostError, AgentHostResult
from app.application.learning_agents import WorkflowGraphDraft
from tests.test_learning_agents import pipeline, data


@pytest.mark.parametrize("repaired", [True, False])
async def test_graph_transport_repair_uses_existing_single_budget(monkeypatch, repaired):
    worker = await pipeline()
    requests = []
    raw = '{"decision":"skip","reason":"done"}}'
    diagnostic = {"kind": "invalid_json", "parseError": "Extra data at position 35",
                  "rawArguments": raw, "truncated": False, "stopReason": "toolUse"}
    async def execute(self, request, **kwargs):
        requests.append(request)
        if len(requests) == 1 or not repaired:
            raise AgentHostError("MODEL_MALFORMED_OUTPUT", "invalid JSON", reported_error={"outputParseError": diagnostic})
        return AgentHostResult(output={"decision": "skip", "reason": "done"})
    monkeypatch.setattr(AgentHost, "execute", execute)
    if repaired:
        assert (await worker.call("workflow", WorkflowGraphDraft, {}, name="compose", phase="graph")).decision == "skip"
    else:
        with pytest.raises(AgentHostError):
            await worker.call("workflow", WorkflowGraphDraft, {}, name="compose", phase="graph")
    assert len(requests) == 2
    feedback = data(requests[1])["validationFeedback"]
    assert feedback["invalidJsonSubmission"] == diagnostic
    assert feedback["previousDraft"] is None
    assert requests[0].max_output_tokens == requests[1].max_output_tokens
    stage = await worker._stage("workflow:compose:graph")
    assert stage["formatRepairCount"] == 1
    if not repaired:
        # A restarted caller has no additional automatic format repair allowance.
        with pytest.raises(AgentHostError):
            await worker.call("workflow", WorkflowGraphDraft, {}, name="compose", phase="graph")
        assert len(requests) == 3


@pytest.mark.parametrize("changes", [{"stopReason": "length"}, {"truncated": True}, {"kind": "conflict"}])
async def test_length_incomplete_and_conflicting_results_do_not_gain_repair(monkeypatch, changes):
    worker = await pipeline()
    calls = []
    diagnostic = {"kind": "invalid_json", "parseError": "bad", "rawArguments": "{}x", "truncated": False, "stopReason": "toolUse", **changes}
    async def execute(self, request, **kwargs):
        calls.append(request)
        raise AgentHostError("MODEL_MALFORMED_OUTPUT", "bad", reported_error={"outputParseError": diagnostic})
    monkeypatch.setattr(AgentHost, "execute", execute)
    with pytest.raises(AgentHostError):
        await worker.call("workflow", WorkflowGraphDraft, {}, name="compose", phase="graph")
    assert len(calls) == 1
    assert not (await worker._stage("workflow:compose:graph")).get("formatRepairCount")
