import pytest
from jsonschema import Draft202012Validator

from app.application.extraction_graph_contract import (
    extraction_node_contract, required_node_inputs, validate_extraction_nodes,
)
from app.domain.workflow import WorkflowNode


@pytest.mark.parametrize("node", [
    {"type": "capability_call", "config": {"capabilityVersionId": "v", "interface": "execute"}},
    {"type": "capability_call", "config": {"capabilityVersionId": "v", "interface": "execute",
                                           "digest": "sha256:d", "extra": True}},
    {"type": "skillx", "config": {"interface": "execute", "generatedSkill": "new", "package": "/host/path"}},
    {"type": "workflow_call", "config": {"workflowVersionId": "child"}},
    {"type": "llm", "config": {}},
    {"type": "agent", "config": {"maxSteps": 1, "allowedCapabilities": [], "outputSchema": {}}},
])
def test_model_and_backend_reject_the_same_unverifiable_node(node):
    assert not Draft202012Validator(extraction_node_contract({})).is_valid(node)
    with pytest.raises(ValueError, match="spec.nodes.0"):
        validate_extraction_nodes({"spec": {"nodes": [node]}}, {})


@pytest.mark.parametrize("node", [
    {"type": "capability_call", "config": {"capabilityVersionId": "v", "interface": "execute", "digest": "sha256:d"}},
    {"type": "skillx", "config": {"interface": "execute", "generatedSkill": "new"}},
    {"type": "skillx", "config": {"interface": "execute", "capabilityVersionId": "v"}},
    {"type": "workflow_call", "config": {"generatedWorkflow": "child"}},
    {"type": "workflow_call", "config": {"workflowVersionId": "v", "digest": "sha256:d"}},
    {"type": "llm", "config": {"prompt": "classify", "outputSchema": {"type": "object"}}},
])
def test_published_and_unpublished_dependency_bindings_remain_supported(node):
    assert Draft202012Validator(extraction_node_contract({})).is_valid(node)
    validate_extraction_nodes({"spec": {"nodes": [node]}}, {})


def test_existing_catalog_inputs_are_required_in_model_and_backend_contract():
    data = {"catalog": {"capabilities": [{"id": "v", "interfaces": {
        "execute": {"inputSchema": {"required": ["repoPath", "branch"]}}}}]}}
    node = {"type": "capability_call", "config": {"capabilityVersionId": "v", "interface": "execute", "digest": "sha256:d"},
            "input": {"repoPath": "${input.repoPath}"}}
    assert required_node_inputs(node, data) == {"repoPath", "branch"}
    with pytest.raises(ValueError, match="required"):
        validate_extraction_nodes({"spec": {"nodes": [node]}}, data)
    assert not Draft202012Validator(extraction_node_contract(data)).is_valid(node)
    node["input"]["branch"] = "${input.branch}"
    validate_extraction_nodes({"spec": {"nodes": [node]}}, data)
    assert Draft202012Validator(extraction_node_contract(data)).is_valid(node)


def test_general_designer_node_model_still_accepts_agent_nodes():
    assert WorkflowNode(id="run", type="agent", displayName="Run", config={}).type == "agent"


@pytest.mark.parametrize('node', [
    {'type': 'condition', 'config': {'operation': 'equals', 'value': '${input.success}', 'expected': True}},
    {'type': 'condition', 'config': {'operation': 'equals'}, 'input': {'expected': True}},
    {'type': 'condition', 'config': {'operation': 'filter'}, 'input': {'value': []}},
    {'type': 'skillx', 'config': {'generatedSkill': 'create', 'interface': 'execute'},
     'input': {'id': {'$optional': '${input.nested.id}'}}},
])
def test_ignored_condition_config_and_unsafe_optional_mapping_rejected(node):
    assert not Draft202012Validator(extraction_node_contract({})).is_valid(node)
    with pytest.raises(ValueError):
        validate_extraction_nodes({'spec': {'nodes': [node]}}, {})


def test_condition_value_and_optional_passthrough_supported():
    nodes = [
        {'type': 'condition', 'config': {'operation': 'equals', 'expected': True}, 'input': {'value': '${read.output.success}'}},
        {'type': 'skillx', 'config': {'generatedSkill': 'create', 'interface': 'execute'}, 'input': {'id': {'$optional': '${input.id}'}}},
    ]
    validate_extraction_nodes({'spec': {'nodes': nodes}}, {})
    for node in nodes:
        Draft202012Validator(extraction_node_contract({})).validate(node)


@pytest.mark.parametrize('mapping', [
    {'$optional': '${input.id}'},
    {'nested': {'id': {'$optional': '${input.id}'}}},
    {'items': [{'$optional': '${input.id}'}]},
])
def test_optional_invalid_positions_rejected_by_model_and_backend(mapping):
    node = {'type': 'transform', 'input': mapping}
    assert not Draft202012Validator(extraction_node_contract({})).is_valid(node)
    with pytest.raises(ValueError):
        validate_extraction_nodes({'spec': {'nodes': [node]}}, {})
