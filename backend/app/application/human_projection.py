"""Durable human cards for workflow approvals and recovery questions."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.application.compiler import digest_json
from app.infrastructure.database import Approval, Run, utcnow
from app.infrastructure.models.tasks import HumanInteraction, Invocation, Task


async def sync_workflow_interactions(session):
    from app.application.tasks import emit

    for approval in await session.scalars(select(Approval).where(Approval.status == "pending")):
        run = await session.get(Run, approval.run_id)
        if not run or run.status in {"succeeded", "failed", "cancelled"}:
            continue
        call = await session.scalar(select(Invocation).where(Invocation.run_id == run.id))
        if not call:
            task_id = str(uuid5(NAMESPACE_URL, "manual-run:" + run.id))
            await session.execute(
                insert(Task)
                .values(
                    id=task_id,
                    goal="手动工作流执行",
                    status="manual",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(Invocation)
                .values(
                    id=str(uuid5(NAMESPACE_URL, "manual-invocation:" + run.id)),
                    task_id=task_id,
                    scope=task_id,
                    call_key="manual",
                    kind="workflow",
                    status="waiting_execution",
                    run_id=run.id,
                    request_json={"versionId": run.workflow_version_id, "input": run.input_json},
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
                .on_conflict_do_nothing()
            )
        else:
            task_id = call.task_id
        question = approval.snapshot_json.get("effect") == "recovery.question"
        request = {
            "title": approval.snapshot_json.get("title", "工作流需要确认"),
            "reason": approval.snapshot_json.get("description", ""),
            "operation": approval.snapshot_json,
            "approvalId": approval.id,
            "approvalDigest": approval.snapshot_digest,
            "runId": run.id,
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"answer": {"type": "string", "minLength": 1}}
                if question
                else {"approved": {"type": "boolean"}},
                "required": ["answer" if question else "approved"],
            },
        }
        interaction_id = str(uuid5(NAMESPACE_URL, "approval-card:" + approval.id))
        created = await session.execute(
            insert(HumanInteraction)
            .values(
                id=interaction_id,
                task_id=task_id,
                call_key="approval:" + approval.id,
                kind="information" if question else "authorization",
                status="pending",
                request_json=request,
                snapshot_digest=digest_json(request),
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            .on_conflict_do_nothing()
        )
        if created.rowcount:
            emit(
                session,
                task_id,
                "human.requested",
                {"interactionId": interaction_id, "request": request},
            )
    cards = await session.scalars(
        select(HumanInteraction).where(
            HumanInteraction.status == "pending", HumanInteraction.call_key.like("approval:%")
        )
    )
    for card in cards:
        approval = await session.get(Approval, card.request_json["approvalId"])
        run = await session.get(Run, card.request_json["runId"])
        if (
            not approval
            or approval.status != "pending"
            or not run
            or run.status in {"succeeded", "failed", "cancelled"}
            or approval.snapshot_digest != card.request_json["approvalDigest"]
        ):
            card.status = "superseded"
            emit(session, card.task_id, "human.superseded", {"interactionId": card.id})

    # Waiting on one branch must not hide active work on another branch.
    from app.infrastructure.database import NodeRun

    task_ids = await session.scalars(
        select(HumanInteraction.task_id).where(HumanInteraction.status == "pending").distinct()
    )
    for task_id in task_ids:
        task = await session.get(Task, task_id)
        if not task or task.status in {
            "manual",
            "deciding",
            "succeeded",
            "failed",
            "blocked",
            "cancelled",
        }:
            continue
        active = False
        calls = await session.scalars(select(Invocation).where(Invocation.task_id == task_id))
        for call in calls:
            if call.status in {"queued", "dispatching", "running"}:
                active = True
            elif call.status == "waiting_execution":
                run = await session.get(Run, call.run_id)
                if run and run.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "waiting_approval",
                }:
                    active = True
                elif run and run.status == "waiting_approval":
                    active |= bool(
                        await session.scalar(
                            select(NodeRun.id)
                            .where(
                                NodeRun.run_id == run.id, NodeRun.status.in_(["ready", "running"])
                            )
                            .limit(1)
                        )
                    )
        task.status = "waiting_execution" if active else "waiting_user"


async def answer_workflow_interaction(session, card, answer):
    from app.api.schemas import Decision, RecoveryAnswer
    from app.application.approval_transitions import answer_recovery_question, approve, reject

    approval = await session.get(Approval, card.request_json["approvalId"])
    run = await session.get(Run, card.request_json["runId"])
    if (
        not approval
        or approval.status != "pending"
        or not run
        or run.status in {"succeeded", "failed", "cancelled"}
        or approval.snapshot_digest != card.request_json["approvalDigest"]
        or digest_json(approval.snapshot_json) != approval.snapshot_digest
    ):
        raise ValueError("工作流交互快照已失效")
    if card.kind == "authorization":
        if answer["approved"]:
            await approve(approval.id, Decision(snapshotDigest=approval.snapshot_digest), session)
        else:
            await reject(approval.id, session)
    else:
        await answer_recovery_question(
            approval.id,
            RecoveryAnswer(snapshotDigest=approval.snapshot_digest, answer=answer["answer"]),
            session,
        )
