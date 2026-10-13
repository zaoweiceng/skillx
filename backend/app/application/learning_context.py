"""Keep callable contracts, not unrelated discovery metadata, in builder prompts."""
from copy import deepcopy
import json


def _compact_workflow_process_output(call):
    """Preview oversized process envelopes, never relabel previews as evidence."""
    observed = call.get("observedOutput")
    if not (call.get("kind") == "code" and isinstance(observed, dict)
            and "exitCode" in observed and "stdout" in observed
            and ("argv" in observed or "requestDigest" in observed)):
        return
    stdout = observed.get("stdout")
    if not isinstance(stdout, str) or len(stdout.encode("utf-8")) <= 4096:
        return
    try:
        json.loads(stdout)
    except ValueError:
        pass
    else:
        return  # Canonical structured evidence is handled by normal decoding.
    from app.application.task_context import observation
    call["evidencePreview"] = {
        "partial": True, "status": observed.get("status"), "exitCode": observed.get("exitCode"),
        "stdout": observation(stdout, 1200), "stderr": observation(observed.get("stderr", ""), 200),
        "originalBytes": len(json.dumps(observed, ensure_ascii=False).encode("utf-8")),
        "outputStructure": {key: type(value).__name__ for key, value in observed.items()},
        "omittedPaths": ["/stdout"],
    }
    call["evidenceModeHint"] = (
        "仅上下文预览，不是实际 observedOutput；不可将截断值用作 sourceInput/sourceOutput 的映射值。"
        "如使用此来源提炼通用规则，请显式选择 derived 并用泛化样例实际执行验证；"
        "完整原始证据仍由后端保存。")
    for key in ("observedOutput", "output", "input"):
        call.pop(key, None)


def compact_catalog_context(data: dict) -> dict:
    result = deepcopy(data)
    if "suppliedWorkflowContracts" in result or "suppliedSkillXContracts" in result:
        for call in result.get("calls", []):
            _compact_workflow_process_output(call)
    if isinstance(result.get("fragment"), dict):
        previous = result.get("previousDraft") or {}
        skill = previous.get("skillx", previous) if isinstance(previous, dict) else {}
        if not isinstance(skill, dict):
            skill = {}
        pure = result.get("executionProfile") == "pure-python" or skill.get("executionProfile") == "pure-python"
        for call in result.get("calls", []):
            if call.get("kind") == "code" and "observedOutput" in call:
                # Evidence pointers resolve against this canonical decoded value.
                # Keep it intact, including strings needed for source mappings.
                call.pop("output", None)
                if pure:
                    call.pop("input", None)
    catalog = result.get("catalog")
    if not isinstance(catalog, dict):
        return result
    capabilities = catalog.get("capabilities", [])
    previous = result.get("previousDraft", {})
    graph = previous.get("workflow", {}) if isinstance(previous, dict) else {}
    nodes = graph.get("spec", {}).get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        references = {
            config[key]
            for node in nodes if isinstance(node, dict)
            for config in [node.get("config") or {}]
            for key in ("capabilityVersionId", "workflowVersionId")
            if isinstance(config.get(key), str)
        }
        available = {item.get("id") for item in capabilities}
        # A missing reference is itself repair feedback: retain discovery data
        # so the Agent can replace it, rather than guessing another capability.
        if references and references.issubset(available):
            capabilities = [item for item in capabilities if item.get("id") in references]
    for item in capabilities:
        # These are discovery examples, not validation evidence. They can also
        # contain old task-specific names that must not leak into new fixtures.
        item.pop("examples", None)
        item.pop("tags", None)
        if item.get("displayName") == item.get("name"):
            item.pop("displayName", None)
        interfaces = item.get("interfaces", {})
        # The planner's discovery catalog intentionally contains only names.
        for interface in interfaces.values() if isinstance(interfaces, dict) else []:
            if interface.get("description") == item.get("description"):
                interface.pop("description", None)
            if interface.get("uiSchema") == {}:
                interface.pop("uiSchema", None)
    catalog["capabilities"] = capabilities
    return result
