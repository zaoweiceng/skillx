from uuid import uuid4

import pytest

from app.api.routes import ContractPatchApply, apply_workflow_contract_patch
from app.infrastructure.database import SessionLocal, Workflow
from app.runtime.contract_patch import ContractPatchViolation, apply_contract_patch


def _browser_definition() -> dict:
    contract = {
        "contractVersion": "1",
        "urlPattern": "https://forms.example.org/apply*",
        "pageIdentity": {"identity": "user-1", "businessObject": "expense"},
        "fields": [{"id": "amount", "required": True, "type": "number"}],
        "submit": {"role": "button", "name": "提交"},
    }
    return {
        "apiVersion": "workflow.platform/v1",
        "kind": "Workflow",
        "metadata": {"name": "expense"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "nodes": [
                {"id": "input", "type": "input", "displayName": "输入"},
                {
                    "id": "submit",
                    "type": "browser",
                    "displayName": "提交",
                    "config": {"pageContract": contract, "submit": True},
                    "sideEffect": True,
                    "idempotent": False,
                },
                {"id": "output", "type": "output", "displayName": "输出"},
            ],
            "edges": [
                {"source": "input", "target": "submit"},
                {"source": "submit", "target": "output"},
            ],
        },
    }


def _patch() -> dict:
    return {
        "kind": "PageContractPatch",
        "baseContractVersion": "1",
        "proposedFields": [
            {"id": "amount", "required": True, "type": "number"},
            {"id": "memo", "required": True, "type": "text"},
        ],
        "differences": ["新增必填字段 memo"],
        "status": "draft",
    }


def test_apply_patch_adjusts_fields_and_bumps_version() -> None:
    patched = apply_contract_patch(_browser_definition(), "submit", _patch())
    contract = patched["spec"]["nodes"][1]["config"]["pageContract"]
    assert contract["contractVersion"] == "2"
    assert {field["id"] for field in contract["fields"]} == {"amount", "memo"}
    # L4 安全域字段不变。
    assert contract["urlPattern"] == "https://forms.example.org/apply*"
    assert contract["pageIdentity"] == {"identity": "user-1", "businessObject": "expense"}


def test_apply_patch_rejects_stale_base_version() -> None:
    patch = _patch()
    patch["baseContractVersion"] = "99"
    with pytest.raises(ContractPatchViolation):
        apply_contract_patch(_browser_definition(), "submit", patch)


def test_apply_patch_rejects_non_browser_node() -> None:
    with pytest.raises(ContractPatchViolation):
        apply_contract_patch(_browser_definition(), "input", _patch())


async def test_contract_patch_endpoint_updates_draft_not_published() -> None:
    async with SessionLocal() as session, session.begin():
        workflow = Workflow(
            id=str(uuid4()),
            name="expense",
            draft_revision=1,
            draft_json=_browser_definition(),
        )
        session.add(workflow)
        workflow_id = workflow.id

    async with SessionLocal() as session:
        result = await apply_workflow_contract_patch(
            workflow_id,
            ContractPatchApply(nodeId="submit", patch=_patch()),
            session,
        )
    assert result["draftRevision"] == 2
    contract = result["definition"]["spec"]["nodes"][1]["config"]["pageContract"]
    assert contract["contractVersion"] == "2"
