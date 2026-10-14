from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.compiler import digest_json
from app.application.execution_receipts import build_execution_receipt, output_coverage
from app.infrastructure.database import utcnow
from tests.fixtures import deterministic_workflow


def record(node_id, output, status="succeeded"):
    return SimpleNamespace(
        node_id=node_id,
        node_type="transform",
        attempt=1,
        output_json=output,
        status=status,
        finished_at=utcnow(),
    )


def test_coverage_uses_exact_nested_output_references_and_actual_values():
    node = record("step", {"items": [{"value": 7}], "clean": True})
    mapping = {
        "nested": [{"value": "${step.output.items[0].value}"}],
        "clean": "${step.output.clean}",
        "text": "prefix ${step.output.clean}",
        "input": "${input.name}",
        "wrong": "${step.output.clean}",
        "absent": "${step.output.missing}",
    }
    actual = {
        "nested": [{"value": 7}],
        "clean": True,
        "text": "prefix True",
        "input": "Ada",
        "wrong": False,
        "absent": None,
    }
    assert output_coverage(mapping, actual, {"step": node}) == [
        {
            "outputPath": ["nested", 0, "value"],
            "nodeId": "step",
            "nodeOutputPath": "items[0].value",
        },
        {"outputPath": ["clean"], "nodeId": "step", "nodeOutputPath": "clean"},
    ]


def test_failed_node_or_display_name_never_claims_coverage():
    node = record("verify", {"clean": True}, status="failed")
    assert (
        output_coverage({"clean": "${verify.output.clean}"}, {"clean": True}, {"verify": node})
        == []
    )


def fixture(monkeypatch):
    definition = deterministic_workflow()
    output = {"message": "你好，Ada"}
    invocation = SimpleNamespace(
        id="inv",
        status="succeeded",
        run_id="run",
        output_json=output,
        request_json={"versionId": "version", "input": {"name": "Ada"}},
    )
    run = SimpleNamespace(
        id="run",
        status="succeeded",
        finished_at=utcnow(),
        cancellation_requested_at=None,
        input_json={"name": "Ada"},
        output_json=output,
    )
    nodes = [
        record("input", {"name": "Ada"}),
        record("format", {"text": "你好，Ada"}),
        record("output", output),
    ]
    nodes[0].node_type = "input"
    nodes[2].node_type = "output"
    source = SimpleNamespace(
        id="version", digest="locked", definition_json=definition, validation_json={}
    )
    monkeypatch.setattr(
        "app.application.execution_receipts.load_execution_source", AsyncMock(return_value=source)
    )
    session = AsyncMock()
    session.get.return_value = run
    session.scalars.return_value = nodes
    return session, invocation, run, nodes


@pytest.mark.parametrize(
    "change", ["invocation", "run", "output", "node", "output-node", "cancelled", "input"]
)
async def test_no_reusable_receipt_for_incomplete_or_mismatched_execution(change, monkeypatch):
    session, invocation, run, nodes = fixture(monkeypatch)
    if change == "invocation":
        invocation.status = "failed"
    elif change == "input":
        invocation.request_json["input"] = {"name": "changed"}
    elif change == "run":
        run.status = "running"
    elif change == "output":
        invocation.output_json = {"message": "different"}
    elif change == "node":
        nodes[1].status = "failed"
    elif change == "output-node":
        nodes[2].output_json = {"message": "different"}
    elif change == "cancelled":
        run.cancellation_requested_at = utcnow()
    assert await build_execution_receipt(session, invocation) is None


async def test_api_result_exposes_receipt_without_expanding_public_business_output(monkeypatch):
    from app.api.tasks import invocation_result_view

    session, invocation, run, nodes = fixture(monkeypatch)
    invocation.kind = "workflow"
    invocation.request_json = {"versionId": "version", "input": run.input_json}
    invocation.task_id = None
    invocation.error_json = None
    invocation.created_at = utcnow()
    result = await invocation_result_view(session, invocation)
    receipt = result["executionReceipt"]
    assert result["output"] == {"message": "你好，Ada"}
    assert receipt["coverage"] == [
        {"outputPath": ["message"], "nodeId": "format", "nodeOutputPath": "text"}
    ]
    assert receipt["inputDigest"] == digest_json(run.input_json)
    assert receipt["outputDigest"] == digest_json(invocation.output_json)
    assert receipt["source"] == {"versionId": "version", "digest": "locked"}
    assert receipt["completedAt"]
    assert "checks" not in receipt


@pytest.mark.parametrize("change", ["none", "mismatch", "failed", "cycle", "llm"])
def test_output_provenance_follows_nested_passthrough_without_inventing_checks(change):
    from app.domain.workflow import WorkflowNode

    records = {
        "actual": record("actual", {"items": [{"sha": "value"}]}),
        "inner": record("inner", {"renamed": [{"sha": "value"}]}),
        "outer": record("outer", {"result": [{"sha": "value"}]}),
    }
    definitions = {
        key: WorkflowNode.model_validate(
            {
                "id": key,
                "type": "transform",
                "displayName": key,
                "input": mapping,
                "config": {"operation": "passthrough"},
            }
        )
        for key, mapping in {
            "inner": {"renamed": "${actual.output.items}"},
            "outer": {"result": "${inner.output.renamed}"},
        }.items()
    }
    if change == "mismatch":
        records["actual"].output_json = {"items": [{"sha": "different"}]}
    elif change == "failed":
        records["actual"].status = "failed"
    elif change == "cycle":
        definitions["inner"].input = {"renamed": "${outer.output.result}"}
    elif change == "llm":
        definitions["inner"] = WorkflowNode.model_validate(
            {
                "id": "inner",
                "type": "llm",
                "displayName": "not a validation proof",
                "input": {},
                "config": {},
            }
        )
        records["inner"].node_type = "llm"
    coverage = output_coverage(
        {"sha": "${outer.output.result[0].sha}"}, {"sha": "value"}, records, definitions
    )
    assert coverage[0]["nodeId"] == (
        "actual"
        if change == "none"
        else "inner"
        if change in {"mismatch", "failed", "llm"}
        else "outer"
    )
    assert "checks" not in coverage[0]
