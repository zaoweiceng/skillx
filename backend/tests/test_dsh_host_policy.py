import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.dsh_host_policy import HostPolicyGrant, grant_host_policy, validate_local_plan
from app.application.compiler import digest_json


def fixture():
    plan = {
        "source": {
            "definition": {
                "spec": {
                    "nodes": [{"id": "input", "type": "input"}, {"id": "execute", "type": "skillx"}]
                }
            }
        },
        "operations": [{"nodeId": "execute", "effects": [{"type": "local.command"}]}],
    }
    plan["source"]["validation"] = {
        "capabilityLocks": [
            {
                "nodeId": "execute",
                "capabilityVersionId": "v",
                "interface": "execute",
                "digest": "locked",
            }
        ]
    }
    plan["operations"][0].update(
        operation={"versionId": "v", "interface": "execute"}, versionDigest="locked"
    )
    policy = {
        "sessionId": "session-1",
        "mode": "danger-full-access",
        "workspaceRoot": "/fixture",
        "approvalPolicy": "never",
    }
    snapshot = {"effect": "workflow.run", "plan": plan}
    approval = SimpleNamespace(id="approval", run_id="run", snapshot_json=snapshot)
    invocation = SimpleNamespace(
        id="invocation", call_key="dsh:" + hashlib.sha256(b"session-1").hexdigest() + ":key"
    )
    body = HostPolicyGrant(
        snapshotDigest=digest_json(snapshot),
        planDigest=digest_json(plan),
        host="dsh",
        callId="call",
        policyDigest=digest_json(policy),
        **policy,
    )
    return plan, approval, invocation, body


@pytest.mark.parametrize("scope", ["network.send", "account.write", "publish", "unknown", None])
def test_external_effects_are_not_local_host_grants(scope):
    plan, _, _, _ = fixture()
    plan["operations"][0]["effects"][0]["type"] = scope
    with pytest.raises(HTTPException):
        validate_local_plan(plan)


@pytest.mark.parametrize("kind", ["agent", "browser", "mcp", "workflow_call", "capability_call"])
def test_dynamic_and_nested_steps_are_not_local_host_grants(kind):
    plan, _, _, _ = fixture()
    plan["source"]["definition"]["spec"]["nodes"][1]["type"] = kind
    with pytest.raises(HTTPException):
        validate_local_plan(plan)


@pytest.mark.parametrize("change", ["session", "policy", "workspace", "digest", "plan", "dynamic"])
async def test_standing_grant_rechecks_binding_and_evidence(change, monkeypatch):
    plan, approval, invocation, body = fixture()
    if change == "session":
        invocation.call_key = "dsh:other:key"
    elif change == "policy":
        body.policy_digest = "changed"
    elif change == "workspace":
        body.workspace_root = "relative"
        body.policy_digest = digest_json(body.policy())
    elif change == "digest":
        body.plan_digest = "changed"
    elif change == "plan":
        plan = deepcopy(plan)
        plan["changed"] = True
    elif change == "dynamic":
        plan["source"]["definition"]["spec"]["nodes"][1]["fallback"] = {"target": "extra"}
        body.plan_digest = digest_json(plan)
    session = AsyncMock()
    session.get.return_value = invocation
    monkeypatch.setattr("app.api.dsh_host_policy.bound_approval", AsyncMock(return_value=approval))
    monkeypatch.setattr("app.api.dsh_host_policy.approval_plan", AsyncMock(return_value=plan))
    transition = AsyncMock()
    monkeypatch.setattr("app.application.approval_transitions.approve", transition)
    with pytest.raises(HTTPException):
        await grant_host_policy(invocation.id, approval.id, body, session)
    transition.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_standing_grant_records_host_policy_separately(monkeypatch):
    plan, approval, invocation, body = fixture()
    session = AsyncMock()
    session.get.return_value = invocation
    monkeypatch.setattr("app.api.dsh_host_policy.bound_approval", AsyncMock(return_value=approval))
    monkeypatch.setattr("app.api.dsh_host_policy.approval_plan", AsyncMock(return_value=plan))
    transition = AsyncMock(return_value={"status": "approved"})
    monkeypatch.setattr("app.application.approval_transitions.approve", transition)
    await grant_host_policy(invocation.id, approval.id, body, session)
    provenance = transition.await_args.kwargs["provenance"]
    assert provenance["source"] == "host-policy"
    assert provenance["policy"]["approvalPolicy"] == "never"
    assert provenance["invocationId"] == invocation.id
    assert provenance["snapshotDigest"] == body.snapshot_digest
    session.commit.assert_awaited_once()


def test_static_grant_only_covers_listed_subset_not_future_dynamic_steps():
    plan, _, _, _ = fixture()
    plan["source"]["definition"]["spec"]["nodes"].append({"id": "future", "type": "skillx"})
    validate_local_plan(plan)
    assert [item["nodeId"] for item in plan["operations"]] == ["execute"]


@pytest.mark.parametrize("change", ["missing", "digest"])
def test_static_host_grant_requires_exact_dependency_lock(change):
    plan, _, _, _ = fixture()
    if change == "missing":
        plan["source"]["validation"]["capabilityLocks"] = []
    else:
        plan["source"]["validation"]["capabilityLocks"][0]["digest"] = "changed"
    with pytest.raises(HTTPException):
        validate_local_plan(plan)


@pytest.mark.parametrize("change", ["none", "drift", "conflict", "external", "model"])
async def test_runtime_operation_host_grant_and_boundaries(change, monkeypatch):
    _, approval, invocation, body = fixture()
    context = {
        "nodeId": "execute",
        "nodeRunId": "node-run",
        "kind": "direct",
        "capabilities": [],
        "effects": [{"type": "local.command"}],
        "upstream": [{"outputDigest": "upstream"}],
    }
    approval.node_run_id = "node-run"
    approval.snapshot_json = {
        "effect": "skillx.invoke",
        "operationContext": context,
        "operation": {"versionId": "v", "interface": "execute", "input": {"sha": "actual"}},
    }
    body.plan_digest = None
    body.authorization_digest = digest_json(context)
    if change == "conflict":
        body.plan_digest = "conflict"
    actual = deepcopy(context)
    if change == "drift":
        actual["upstream"][0]["outputDigest"] = "changed"
    elif change == "external":
        actual["effects"] = [{"type": "network.publish"}]
        approval.snapshot_json["operationContext"] = actual
        body.authorization_digest = digest_json(actual)
    elif change == "model":
        actual["kind"] = "agent"
        approval.snapshot_json["operationContext"] = actual
        body.authorization_digest = digest_json(actual)
    session = AsyncMock()
    session.get.return_value = invocation
    monkeypatch.setattr("app.api.dsh_host_policy.bound_approval", AsyncMock(return_value=approval))
    monkeypatch.setattr("app.api.dsh_host_policy.operation_context", AsyncMock(return_value=actual))
    transition = AsyncMock(return_value={"status": "approved"})
    monkeypatch.setattr("app.application.approval_transitions.approve", transition)
    if change == "none":
        await grant_host_policy(invocation.id, approval.id, body, session)
        evidence = transition.await_args.kwargs["provenance"]
        assert evidence["effect"] == "skillx.invoke"
        assert evidence["authorizationDigest"] == body.authorization_digest
        assert "planDigest" not in evidence
    else:
        with pytest.raises(HTTPException):
            await grant_host_policy(invocation.id, approval.id, body, session)
        transition.assert_not_awaited()
