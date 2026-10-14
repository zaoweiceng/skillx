from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.dsh import answer_invocation, approve_invocation, reject_invocation
from app.api.schemas import Decision
from app.api.tasks import HumanAnswer
from app.application.compiler import digest_json
from app.infrastructure.database import Approval
from app.infrastructure.models.tasks import HumanInteraction, Invocation


def boundary():
    snapshot = {"effect": "workflow.run", "runId": "run-1", "plan": {"input": {"repoPath": "/fixture"}}}
    approval = SimpleNamespace(id="approval-1", run_id="run-1", status="pending",
                               snapshot_json=snapshot, snapshot_digest=digest_json(snapshot))
    invocation = SimpleNamespace(id="invocation-1", run_id="run-1", task_id="task-1", status="running")
    session = AsyncMock()
    async def get(model, key):
        return {(Invocation, invocation.id): invocation, (Approval, approval.id): approval}.get((model, key))
    session.get.side_effect = get
    return session, invocation, approval


@pytest.mark.parametrize("action", [approve_invocation, reject_invocation])
@pytest.mark.parametrize("mismatch", ["run", "terminal", "digest", "mutated", "answered", "missing"])
async def test_host_decision_rejects_unbound_or_stale_snapshot(action, mismatch, monkeypatch):
    session, invocation, approval = boundary()
    body = Decision(snapshotDigest=approval.snapshot_digest)
    if mismatch == "run": invocation.run_id = "other-run"
    if mismatch == "terminal": invocation.status = "cancelled"
    if mismatch == "digest": body.snapshot_digest = "old-digest"
    if mismatch == "mutated": approval.snapshot_json["plan"]["input"]["repoPath"] = "/other"
    if mismatch == "answered": approval.status = "approved"
    if mismatch == "missing": invocation.id = "other-invocation"
    transition = AsyncMock()
    monkeypatch.setattr("app.application.approval_transitions.approve", transition)
    monkeypatch.setattr("app.application.approval_transitions.reject", transition)
    with pytest.raises(HTTPException) as exc:
        await action("invocation-1", "approval-1", body, session)
    assert exc.value.status_code in {404, 409}
    transition.assert_not_awaited()
    session.commit.assert_not_awaited()


async def test_matching_host_decision_delegates_to_canonical_transition(monkeypatch):
    session, invocation, approval = boundary()
    transition = AsyncMock(return_value={"status": "approved"})
    monkeypatch.setattr("app.application.approval_transitions.approve", transition)
    body = Decision(snapshotDigest=approval.snapshot_digest)
    assert await approve_invocation(invocation.id, approval.id, body, session) == {"status": "approved"}
    transition.assert_awaited_once_with(approval.id, body, session)
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("mismatch", ["invocation", "task", "kind", "projection"])
async def test_skillx_authorization_is_scoped_to_exact_invocation(monkeypatch, mismatch):
    session, invocation, _ = boundary()
    item = SimpleNamespace(id="human-1", kind="authorization", task_id=invocation.task_id,
                           request_json={"invocationId": invocation.id})
    if mismatch == "invocation": item.request_json["invocationId"] = "another"
    if mismatch == "task": item.task_id = "another"
    if mismatch == "kind": item.kind = "question"
    if mismatch == "projection": item.request_json["approvalId"] = "another"
    async def get(model, key):
        return invocation if model is Invocation else item if model is HumanInteraction else None
    session.get.side_effect = get
    answer = AsyncMock()
    monkeypatch.setattr("app.api.tasks.answer", answer)
    body = HumanAnswer(revision=1, snapshotDigest="snapshot", idempotencyKey="once", answer={"approved": True})
    with pytest.raises(HTTPException) as exc:
        await answer_invocation(invocation.id, item.id, body, session)
    assert exc.value.status_code == 409
    answer.assert_not_awaited()
