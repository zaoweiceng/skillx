"""Provider JSON Schema and backend accept/reject the same decision shapes."""
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.application.task_contracts import TaskDecision

CALL = {"key": "read", "kind": "code", "arguments": {
    "language": "python", "source": "print('ok')",
}, "reason": "Read the required evidence"}
QUESTION = {"key": "choice", "title": "Choose", "reason": "Missing user choice",
            "inputSchema": {"type": "object", "properties": {}}}


def test_action_tool_projection_preserves_validation_and_saved_criteria():
    from app.application.task_contracts import task_decision_schema

    schema = task_decision_schema([{"id": "evidence-1", "status": "succeeded"}],
                                  {"completionCriteria": ["original acceptance"]})
    script = """
import { resultTools } from './bridges/pi/result-tools.mjs';
import { readFileSync } from 'node:fs';
const tools = resultTools(JSON.parse(readFileSync(0, 'utf8')));
console.log(JSON.stringify(tools.map(tool => ({...tool,
  sample: tool.normalize({answer: 'A different question', evidence: ['evidence-1']})
}))));
"""
    tools = json.loads(subprocess.run(
        ["node", "--input-type=module", "-e", script], input=json.dumps(schema),
        text=True, capture_output=True, check=True, cwd=Path(__file__).parents[2],
    ).stdout)
    by_name = {tool["name"]: tool for tool in tools}
    complete = by_name["task_complete"]
    assert complete["sample"]["completionCriteria"] == ["original acceptance"]
    assert Draft202012Validator(schema).is_valid(complete["sample"])
    validator = Draft202012Validator(by_name["task_dispatch"]["parameters"])
    base = {"summary": "read", "completionCriteria": ["deliver"], "evidenceGap": "missing fact"}
    for call, valid in [
        (CALL, True),
        ({key: value for key, value in CALL.items() if key != "reason"}, True),
        ({**CALL, "reason": ""}, True),
        ({**CALL, "reason": None}, False),
        ({**CALL, "arguments": {"input": CALL["arguments"]}}, False),
        ({**CALL, "arguments": {"language": "shell", "source": "pwd"}}, False),
        ({**CALL, "arguments": {"language": "python", "source": " "}}, False),
        ({**CALL, "kind": "tool", "arguments": {"capabilityRef": "example"}}, True),
        ({**CALL, "kind": "unknown"}, False),
    ]:
        assert validator.is_valid({**base, "calls": [call]}) is valid
    assert not validator.is_valid({**base, "calls": []})
    assert validator.is_valid({"summary": "read", "completionCriteria": ["deliver"],
                              "calls": [CALL]})
    validator = Draft202012Validator(complete["parameters"])
    assert validator.is_valid({"answer": "ok", "evidence": ["unknown-id"]})
    assert not Draft202012Validator(schema).is_valid({
        **complete["sample"], "evidence": ["unknown-id"],
    })
    assert not validator.is_valid({"answer": "ok", "evidence": ["evidence-1"], "calls": [CALL]})


def test_action_tool_schemas_are_stable_across_execution_state():
    from app.application.task_contracts import task_decision_schema

    states = [
        ([], {}),
        ([{"id": "one", "status": "succeeded"}], {"completionCriteria": ["first"]}),
        ([{"id": "other", "status": "succeeded"},
          {"id": "two", "status": "succeeded"},
          {"id": "failed", "status": "failed"}], {"completionCriteria": ["different", "criteria"]}),
    ]
    schemas = [task_decision_schema(calls, state) for calls, state in states]
    script = """
import { resultTools } from './bridges/pi/result-tools.mjs';
import { readFileSync } from 'node:fs';
const schemas = JSON.parse(readFileSync(0, 'utf8'));
const original = JSON.stringify(schemas);
const projections = schemas.map(schema => resultTools(schema).map(tool => ({
  name: tool.name, description: tool.description, parameters: tool.parameters,
})));
if (JSON.stringify(schemas) !== original) throw new Error('Canonical schema mutated');
console.log(JSON.stringify(projections));
"""
    projections = json.loads(subprocess.run(
        ["node", "--input-type=module", "-e", script], input=json.dumps(schemas),
        text=True, capture_output=True, check=True, cwd=Path(__file__).parents[2],
    ).stdout)
    assert projections[0] == projections[1] == projections[2]
    tools = {tool["name"]: tool for tool in projections[0]}
    complete = {"answer": "done", "evidence": ["invented"]}
    assert Draft202012Validator(tools["task_complete"]["parameters"]).is_valid(complete)
    dispatch = {"summary": "read", "completionCriteria": ["deliver"], "calls": [CALL]}
    provider = Draft202012Validator(tools["task_dispatch"]["parameters"])
    assert provider.is_valid(dispatch)
    assert provider.is_valid({**dispatch, "evidenceGap": ""})
    for schema in schemas:
        host = Draft202012Validator(schema)
        assert not host.is_valid({**complete, "action": "complete", "summary": "done",
                                  "completionCriteria": ["deliver"]})
    for schema in schemas[1:]:
        host = Draft202012Validator(schema)
        assert not host.is_valid({**dispatch, "action": "dispatch"})
        assert not host.is_valid({**dispatch, "action": "dispatch", "evidenceGap": ""})


@pytest.mark.parametrize(("fields", "valid"), [
    ({"action": "dispatch"}, False),
    ({"action": "dispatch", "calls": []}, False),
    ({"action": "dispatch", "calls": None}, False),
    ({"action": "dispatch", "calls": [CALL]}, True),
    ({"action": "dispatch", "calls": [CALL], "questions": [QUESTION]}, True),
    ({"action": "dispatch", "calls": [CALL] * 9}, False),
    ({"action": "human.request"}, False),
    ({"action": "human.request", "questions": []}, False),
    ({"action": "human.request", "questions": [QUESTION]}, True),
    ({"action": "human.request", "questions": [QUESTION], "calls": [CALL]}, True),
    ({"action": "complete"}, False),
    ({"action": "complete", "answer": ""}, False),
    ({"action": "complete", "answer": "done"}, True),
    ({"action": "complete", "answer": "done", "calls": [CALL]}, False),
    ({"action": "complete", "answer": "done", "questions": [QUESTION]}, False),
    ({"action": "complete", "answer": "done", "remaining": ["unfinished"]}, False),
    ({"action": "blocked"}, True),
    ({"action": "blocked", "calls": [CALL]}, False),
    ({"action": "blocked", "questions": [QUESTION]}, False),
])
def test_action_shape_parity(fields, valid):
    decision = {"summary": "next step", "completionCriteria": ["deliver"], **fields}
    schema = TaskDecision.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(decision) is valid
    if valid:
        TaskDecision.model_validate(decision)
    else:
        with pytest.raises(ValidationError):
            TaskDecision.model_validate(decision)


@pytest.mark.parametrize("arguments", [
    {"input": {"language": "python", "source": "print(1)"}},
    {"language": "python"},
    {"language": "shell", "source": "echo hi"},
    {"language": "python", "source": " \n\t"},
    {"language": "python", "source": 12},
])
def test_code_arguments_match_provider_contract(arguments):
    test_action_shape_parity({
        "action": "dispatch", "calls": [{**CALL, "arguments": arguments}],
    }, False)


def test_human_schema_must_be_object():
    test_action_shape_parity({"action": "human.request", "questions": [
        {**QUESTION, "inputSchema": {"type": "string"}},
    ]}, False)


def test_schema_cannot_mutate_backend_rules():
    schema = TaskDecision.model_json_schema()
    schema["anyOf"][0]["properties"]["calls"]["minItems"] = 0
    test_action_shape_parity({"action": "dispatch", "calls": []}, False)


def test_runtime_key_uniqueness_is_still_checked():
    call = deepcopy(CALL)
    with pytest.raises(ValidationError, match="key 必须唯一"):
        TaskDecision.model_validate({"action": "dispatch", "calls": [CALL, call],
                                     "summary": "duplicate", "completionCriteria": ["done"]})


def test_invalid_dispatch_feedback_is_specific_and_does_not_echo_model_output():
    from app.application.agent_host import output_schema_feedback

    feedback = output_schema_feedback(TaskDecision.model_json_schema(), {
        "action": "dispatch", "summary": "private content" * 1000,
        "completionCriteria": ["deliver"],
    })
    assert 'required ["calls"]' in feedback
    assert "private content" not in feedback
    assert "questions" not in feedback
    assert "answer" not in feedback


async def test_host_rejects_invalid_dispatch_before_execution_and_keeps_usage(tmp_path):
    from app.application.agent_host import (
        AgentHost,
        AgentHostRequest,
        AgentOutputValidationError,
    )

    class Adapter:
        async def events(self, run_id, request):
            assert not Draft202012Validator(request.output_schema).is_valid({
                "action": "dispatch", "summary": "will execute", "completionCriteria": ["done"],
            })
            yield {"type": "agent.step.completed", "payload": {
                "step": 1, "inputTokens": 20, "outputTokens": 5,
            }}
            yield {"type": "agent.completed", "payload": {
                "steps": 1, "totalTokens": 25, "usageComplete": True,
                "output": {"action": "dispatch", "summary": "will execute",
                           "completionCriteria": ["done"]},
            }}

    with pytest.raises(AgentOutputValidationError) as raised:
        await AgentHost(adapter=Adapter()).execute(AgentHostRequest(
            run_id="invalid-dispatch", prompt="do the task", working_directory=tmp_path,
            output_schema=TaskDecision.model_json_schema(), include_managed_skills=False,
        ))
    assert raised.value.total_tokens == 25
    assert raised.value.input_tokens == 20
    assert raised.value.output_tokens == 5
    assert raised.value.usage_complete
    assert 'required ["calls"]' in str(raised.value)


def test_provider_can_compile_each_branch_without_inheriting_parent_constraints():
    schema = TaskDecision.model_json_schema()
    for branch in schema["anyOf"]:
        action = branch["properties"]["action"]["const"]
        independent = {**branch, "$defs": schema["$defs"]}
        validator = Draft202012Validator(independent)
        assert not validator.is_valid({"action": action})
        assert {"action", "summary", "completionCriteria"} <= set(branch["required"])
        if action == "dispatch":
            assert not validator.is_valid({"action": action, "summary": "next",
                                           "completionCriteria": ["done"]})
    for branch in schema["$defs"]["TaskCall"]["anyOf"]:
        assert set(branch["required"]) == {"key", "kind", "arguments"}


@pytest.mark.parametrize("source", ["print(123)", "import json\nprint(json.dumps({'ok': True}))\n"])
def test_code_pattern_also_allows_full_programs_in_provider_regex_engines(source):
    import re

    from app.application.task_contracts import CODE_ARGUMENTS

    assert re.fullmatch(CODE_ARGUMENTS["properties"]["source"]["pattern"], source)
    test_action_shape_parity({
        "action": "dispatch", "calls": [{**CALL, "arguments": {
            "language": "python", "source": source,
        }}],
    }, True)


def test_runtime_evidence_enum_and_followup_gap():
    from app.application.task_contracts import task_decision_schema

    schema = task_decision_schema([{"id": "known-id", "status": "succeeded"}])
    validator = Draft202012Validator(schema)
    complete = {"action": "complete", "summary": "done", "completionCriteria": ["done"],
                "answer": "answer", "evidence": ["known-id"]}
    assert validator.is_valid(complete)
    assert not validator.is_valid({**complete, "evidence": ["known-id: explanation"]})
    assert not validator.is_valid({**complete, "evidence": ["invented-id"]})
    assert not validator.is_valid({**complete, "evidence": []})
    dispatch = {"action": "dispatch", "summary": "read", "completionCriteria": ["answer"],
                "calls": [{"key": "read", "kind": "code", "reason": "missing fact",
                           "arguments": {"language": "python", "source": "print(1)"}}]}
    assert not validator.is_valid(dispatch)
    assert validator.is_valid({**dispatch, "evidenceGap": "用户问执行结果，现有证据只有规范"})
    assert Draft202012Validator(task_decision_schema([])).is_valid(dispatch)


def test_missing_call_reason_defaults_without_changing_execution_or_evidence_gate():
    from app.application.task_contracts import task_decision_schema

    call = {key: value for key, value in CALL.items() if key != "reason"}
    decision = {"action": "dispatch", "summary": "read", "completionCriteria": ["answer"],
                "calls": [call]}
    parsed = TaskDecision.model_validate(decision)
    assert parsed.calls[0].reason == ""
    assert parsed.calls[0].arguments == CALL["arguments"]
    assert parsed.calls[0].key == CALL["key"]
    assert parsed.calls[0].kind == CALL["kind"]
    validator = Draft202012Validator(task_decision_schema([{"id": "id", "status": "succeeded"}]))
    assert not validator.is_valid(decision)
    assert not validator.is_valid({**decision, "evidenceGap": ""})
    assert validator.is_valid({**decision, "evidenceGap": "缺少目标文件内容，需执行读取"})
    # User-facing questions still require a real reason, unlike execution labels.
    with pytest.raises(ValidationError):
        TaskDecision.model_validate({"action": "human.request", "summary": "ask",
                                     "completionCriteria": ["answer"], "questions": [
                                         {k: v for k, v in QUESTION.items() if k != "reason"}]})


def test_historical_evidence_is_optional_for_an_unrelated_new_goal():
    from app.application.task_contracts import task_decision_schema

    calls = [{"id": "old-read", "status": "succeeded"}]
    validator = Draft202012Validator(task_decision_schema(calls, current_call_ids=set()))
    answer = {"action": "complete", "summary": "回答", "completionCriteria": ["回答"],
              "answer": "你好"}
    assert validator.is_valid(answer)
    assert validator.is_valid({**answer, "evidence": ["old-read"]})
    assert not validator.is_valid({**answer, "evidence": ["invented"]})
    required = Draft202012Validator(task_decision_schema(calls, current_call_ids={"old-read"}))
    assert not required.is_valid(answer)
    assert required.is_valid({**answer, "evidence": ["old-read"]})
