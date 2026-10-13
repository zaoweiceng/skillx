from copy import deepcopy

from app.application.learning_context import compact_catalog_context


def capability(id):
    return {"id": id, "name": id, "displayName": id, "description": "scope",
            "digest": "sha256:locked", "examples": ["old task branch"],
            "interfaces": {"execute": {"description": "scope", "inputSchema": {"required": ["repo"]},
                "outputSchema": {"type": "object"}, "executionProfile": "local-command",
                "permissions": {"required": ["local"]}, "effects": [{"type": "local.command"}]}}}


def test_repair_catalog_keeps_only_exact_referenced_contracts_without_mutating_source():
    data = {"catalog": {"capabilities": [capability("needed"), capability("unrelated")]},
            "previousDraft": {"workflow": {"spec": {"nodes": [
                {"type": "capability_call", "config": {"capabilityVersionId": "needed"}}]}}}}
    before = deepcopy(data)
    compact = compact_catalog_context(data)
    assert data == before
    assert [item["id"] for item in compact["catalog"]["capabilities"]] == ["needed"]
    kept = compact["catalog"]["capabilities"][0]
    assert kept["digest"] == "sha256:locked"
    assert "examples" not in kept and "displayName" not in kept
    expected = before["catalog"]["capabilities"][0]["interfaces"]["execute"]
    assert kept["interfaces"]["execute"] == {k: v for k, v in expected.items() if k != "description"}


def test_unknown_or_no_graph_reference_preserves_catalog_for_agent_choice():
    for previous in ({}, {"workflow": {"spec": {"nodes": [
            {"config": {"workflowVersionId": "not-published"}}]}}}):
        data = {"catalog": {"capabilities": [capability("a"), capability("b")]}, "previousDraft": previous}
        assert len(compact_catalog_context(data)["catalog"]["capabilities"]) == 2


def test_pure_repair_keeps_canonical_evidence_and_fallback_output_intact():
    evidence = {"source": "a/b", "nested": {"value": "must remain exact"}}
    data = {"fragment": {"name": "normalize"}, "previousDraft": {"executionProfile": "pure-python"},
            "calls": [{"id": "decoded", "kind": "code", "status": "succeeded", "input": {"code": "irrelevant"},
                       "output": {"stdout": "duplicate"}, "observedOutput": evidence},
                      {"id": "raw", "kind": "code", "output": {"stdout": "only source"}}]}
    compact = compact_catalog_context(data)
    assert compact["calls"][0] == {"id": "decoded", "kind": "code", "status": "succeeded", "observedOutput": evidence}
    assert compact["calls"][1] == data["calls"][1]
    assert "input" in data["calls"][0]


def test_extractor_keeps_original_commands_for_planning():
    data = {"calls": [{"kind": "code", "input": {"code": "original"},
                       "output": {"stdout": "raw"}, "observedOutput": {"value": "parsed"}}]}
    assert compact_catalog_context(data) == data


def test_workflow_oversized_unparsed_output_is_explicit_preview_not_mapping_evidence():
    structured = {"files": [{"path": "example.py", "diff": "a" * 21000}]}
    process = {"status": "succeeded", "exitCode": 0, "argv": ["python", "source.py"],
               "stdout": "�truncated JSON" + "b" * 33000, "stderr": ""}
    data = {"fragment": {"name": "draft-copy"}, "suppliedSkillXContracts": [], "suppliedWorkflowContracts": [],
            "calls": [{"id": "summary", "kind": "code", "observedOutput": structured},
                      {"id": "large", "kind": "code", "input": {"code": "original"},
                       "observedOutput": process, "output": process}]}
    result = compact_catalog_context(data)
    assert result["calls"][0]["observedOutput"] == structured
    preview = result["calls"][1]
    assert preview["id"] == "large" and "derived" in preview["evidenceModeHint"]
    assert "observedOutput" not in preview and "output" not in preview and "input" not in preview
    assert preview["evidencePreview"]["partial"] is True
    assert preview["evidencePreview"]["omittedPaths"] == ["/stdout"]
    assert len(preview["evidencePreview"]["stdout"]) < 1400
    assert data["calls"][1]["observedOutput"] == process
    # The same raw value remains available to SkillX construction and planning.
    data.pop("suppliedSkillXContracts")
    data.pop("suppliedWorkflowContracts")
    assert compact_catalog_context(data)["calls"][1]["observedOutput"] == process
