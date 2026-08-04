"""005-C：Action Compiler Registry 裁决与 NormalizedAction IR 的正反测试。"""

from __future__ import annotations

import pytest

from app.application.action_compilers import ActionCompilerRegistry, CompileContext
from app.application.distillation_ir import (
    CapabilityRef,
    DistillationProposal,
    InputBinding,
    NormalizedAction,
    ProposedNode,
    TrajectoryAnalysisError,
    build_action_ir,
)

OUTPUT_SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}}}


def _ctx(node_id: str = "n1") -> CompileContext:
    return CompileContext(node_id=node_id, message="q1", output_schema=OUTPUT_SCHEMA)


def test_locked_mcp_compiles_unlocked_stays_agent() -> None:
    registry = ActionCompilerRegistry()
    locked = NormalizedAction(
        actionId="c1",
        kind="mcp",
        capabilityRef=CapabilityRef(
            name="weather", registryId="s1", version="1", digest="sha256:x"
        ),
        inputBindings=[InputBinding(field="city", source="user_input", inputKey="message")],
        effect="none",
        idempotent=True,
    )
    assert registry.compile(locked, _ctx()).decision == "compiled"

    unlocked = NormalizedAction(actionId="c2", kind="mcp", capabilityRef=CapabilityRef(name="w"))
    assert registry.compile(unlocked, _ctx()).decision == "agent"


def test_transform_lookup_requires_evidence() -> None:
    registry = ActionCompilerRegistry()
    good = NormalizedAction(
        actionId="c1",
        kind="transform",
        capabilityRef=CapabilityRef(name="lookup"),
        inputBindings=[InputBinding(field="key", source="user_input", inputKey="message")],
        observations={"operation": "lookup", "table": {"q1": "a"}},
    )
    assert registry.compile(good, _ctx()).decision == "compiled"

    # lookup 声明但缺全表/参数来源：rejected（不可发布），不静默通过。
    bad = NormalizedAction(
        actionId="c2",
        kind="transform",
        capabilityRef=CapabilityRef(name="lookup"),
        inputBindings=[InputBinding(field="key", source="constant", constant="frozen")],
        observations={"operation": "lookup"},
    )
    result = registry.compile(bad, _ctx())
    assert result.decision == "rejected"
    assert result.diagnostics[0]["code"] == "UNPROVEN_TRANSFORM"


def test_unknown_kind_preserved_as_agent_not_dropped() -> None:
    registry = ActionCompilerRegistry()
    action = NormalizedAction(
        actionId="c1", kind="api", capabilityRef=CapabilityRef(name="curl")
    )
    result = registry.compile(action, _ctx())
    assert result.decision == "agent"
    assert result.node is not None and result.node["type"] == "agent"


def test_browser_without_contract_stays_agent() -> None:
    registry = ActionCompilerRegistry()
    action = NormalizedAction(
        actionId="c1", kind="browser", capabilityRef=CapabilityRef(name="browse")
    )
    assert registry.compile(action, _ctx()).decision == "agent"


def test_build_ir_requires_call_id() -> None:
    with pytest.raises(TrajectoryAnalysisError):
        build_action_ir({"prompt": "q1", "actions": [{"capability": "x"}]})


def test_build_ir_forms_dependency_chain() -> None:
    ir = build_action_ir(
        {
            "prompt": "q1",
            "actions": [
                {"callId": "a", "capability": "x", "arguments": {}, "result": {}},
                {"callId": "b", "capability": "y", "arguments": {}, "result": {}},
            ],
        }
    )
    assert [item.action_id for item in ir] == ["a", "b"]
    assert ir[1].depends_on == ["a"]


def test_distillation_proposal_schema_roundtrip() -> None:
    proposal = DistillationProposal(
        nodes=[ProposedNode(nodeId="n1", actionIds=["a"], strategy="deterministic")],
        variableCandidates={"key": "${input.message}"},
    )
    dumped = proposal.model_dump(by_alias=True)
    assert dumped["nodes"][0]["nodeId"] == "n1"
    assert dumped["variableCandidates"]["key"] == "${input.message}"
