"""Model-facing and backend constraints for graphs supported by extraction validation.

The general workflow designer supports more node types. Extraction must promise
only nodes whose execution can be verified before publication.
"""
from jsonschema import Draft202012Validator


EXTRACTION_NODE_TYPES = ["input", "output", "skillx", "llm", "condition", "transform",
                         "capability_call", "workflow_call"]
_NONEMPTY = {"type": "string", "minLength": 1}


def input_binding_contracts(data):
    """Yield config selectors and the exact required inputs of callable contracts."""
    for group, key in (("suppliedSkillXContracts", "generatedSkill"),
                       ("suppliedWorkflowContracts", "generatedWorkflow")):
        for item in data.get(group, []):
            yield {key: item["name"]}, item.get("inputSchema", {}).get("required", [])
    for item in data.get("catalog", {}).get("capabilities", []):
        interfaces = item.get("interfaces", {})
        if not isinstance(interfaces, dict):
            continue
        for name, interface in interfaces.items():
            required = interface.get("inputSchema", {}).get("required", [])
            yield {"capabilityVersionId": item["id"], "interface": name}, required
            # workflow_call has one public execute interface and no interface key.
            if name == "execute":
                yield {"workflowVersionId": item["id"]}, required


def required_node_inputs(node, data):
    config = node.get("config", {})
    return {name for selector, required in input_binding_contracts(data)
            if all(config.get(key) == value for key, value in selector.items()) for name in required}


def extraction_node_contract(data):
    conditions = []

    def config_for(node_type, contract):
        conditions.append({"if": {"properties": {"type": {"const": node_type}}, "required": ["type"]},
                           "then": {"required": ["config"], "properties": {"config": contract}}})

    config_for("capability_call", {"type": "object", "additionalProperties": False,
        "required": ["capabilityVersionId", "interface", "digest"],
        "properties": {key: _NONEMPTY for key in ("capabilityVersionId", "interface", "digest")}})
    config_for("skillx", {"type": "object", "required": ["interface"],
        "properties": {"interface": _NONEMPTY, "capabilityVersionId": _NONEMPTY,
                       "generatedSkill": _NONEMPTY, "package": {"enum": [None, ""]}},
        "anyOf": [{"required": ["capabilityVersionId"]}, {"required": ["generatedSkill"]}]})
    config_for("workflow_call", {"type": "object",
        "properties": {key: _NONEMPTY for key in ("workflowVersionId", "digest", "generatedWorkflow")},
        "anyOf": [{"required": ["workflowVersionId", "digest"]}, {"required": ["generatedWorkflow"]}]})
    config_for("llm", {"type": "object", "required": ["outputSchema"],
                       "properties": {"outputSchema": {"type": "object"}}})
    for selector, required in input_binding_contracts(data):
        if required:
            conditions.append({"if": {"required": ["config"], "properties": {"config": {
                "required": list(selector), "properties": {key: {"const": value} for key, value in selector.items()}}}},
                "then": {"required": ["input"], "properties": {"input": {"required": required}}}})
    return {"properties": {"type": {"enum": EXTRACTION_NODE_TYPES}}, "allOf": conditions}


def validate_extraction_nodes(definition, data):
    validator = Draft202012Validator(extraction_node_contract(data))
    for index, node in enumerate(definition.get("spec", {}).get("nodes", [])):
        errors = list(validator.iter_errors(node))
        if errors:
            # Return actionable paths without copying the rejected graph back
            # into the repair prompt or exposing unrelated catalog data.
            details = [f"spec.nodes.{index}." + ".".join(map(str, error.absolute_path))
                       + f": {error.validator} {error.validator_value}"
                       for error in errors]
            raise ValueError("提取工作流节点契约不一致：" + "; ".join(details))
