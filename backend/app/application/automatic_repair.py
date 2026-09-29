"""Reuse the existing source-bound revision and replay gates in background jobs."""

from sqlalchemy import select

from app.infrastructure.database import FailureObservation, SessionLocal


async def revise_from_failure(version_id: str, observation_id: str | None = None):
    from app.api.routes import apply_version_workflow_patch, propose_failure_revision
    from app.api.schemas import PublishRequest
    from app.application.workflow_publishing import publish_workflow

    async with SessionLocal() as session:
        observation = await session.scalar(
            select(FailureObservation)
            .where(
                FailureObservation.workflow_version_id == version_id,
                FailureObservation.id == observation_id if observation_id else True,
            )
            .order_by(FailureObservation.updated_at.desc())
            .limit(1)
        )
        if observation is None:
            raise ValueError("尚无可复现的失败观测")
        proposal = await propose_failure_revision(observation.id, session)
        draft = await apply_version_workflow_patch(
            version_id,
            {
                "proposalId": proposal["proposalId"],
                "proposalDigest": proposal["proposalDigest"],
                "sourceRunId": observation.last_run_id,
            },
            session,
        )
        published = await publish_workflow(
            draft["draftWorkflowId"],
            PublishRequest(
                draftRevision=draft["draftRevision"],
                draftDigest=draft["draftDigest"],
                replayEvidenceId=draft["replay"]["replayEvidenceId"],
            ),
            session,
        )
        return {
            "workflowVersionId": published["id"],
            "baseVersionId": version_id,
            "replay": draft["replay"],
        }
