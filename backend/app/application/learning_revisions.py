"""Feedback-scoped revisions reuse immutable evidence and unaffected artifacts."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import digest_json
from app.infrastructure.models.tasks import LearningJob


class RevisionConflict(ValueError):
    """The user or another revision changed the approved baseline."""


async def create_revision_job(session, source: LearningJob, feedback: dict[str, str]):
    """Create an idempotent repair checkpoint, not a new extraction of the source task.

    Keys are explicit skillx:<name> or workflow:<name>. Ancestor workflows are
    republished against new dependency locks; their Agent-authored graphs survive.
    """
    if not feedback or any(not value.strip() for value in feedback.values()):
        raise ValueError("修订必须提供具体失败反馈")
    stages = deepcopy(source.result_json.get("agentStages", {}))
    artifacts = deepcopy(source.result_json.get("artifacts", {}))
    for key in feedback:
        if key not in stages or key.split(":", 1)[0] not in {"skillx", "workflow"}:
            raise ValueError("修订目标不是已有 SkillX 或工作流")
    fingerprint = "revision:" + digest_json({"sourceJob": source.id, "artifacts": artifacts, "feedback": feedback})
    existing = await session.scalar(select(LearningJob).where(LearningJob.fingerprint == fingerprint))
    if existing:
        return existing
    plan = stages.get("extractor", {}).get("output", {})
    flows = [*plan.get("workflows", []), *([plan["workflow"]] if plan.get("workflow") else [])]
    affected = {key.split(":", 1)[1] for key in feedback}
    while True:
        children = {flow["name"] for flow in flows if set(flow.get("dependsOn", [])) & affected}
        if children <= affected:
            break
        affected |= children
    bases, flow_bases = {}, {}
    for key, error in feedback.items():
        stage = stages[key]
        stage.update(status="needs_revision", error=error, revisionCount=0)
        # Feedback is new work, but neither previous outputs nor tests are lost.
        stage.pop("validationStatus", None)
        if key.startswith("workflow:"):
            stages[key + ":bindings"] = {"role": "workflow", "name": key.split(":", 1)[1],
                "status": "needs_revision", "error": error}
    for name in affected:
        if name in artifacts.get("skillx", {}):
            bases[name] = artifacts["skillx"].pop(name)
            artifacts.get("skillxSkills", {}).pop(name, None)
        if name in artifacts.get("workflows", {}):
            flow_bases[name] = artifacts["workflows"].pop(name)
    result = {"agentStages": stages, "artifacts": artifacts,
        "revisionBaseVersions": bases, "revisionBaseWorkflowVersions": flow_bases,
        "revisionFeedback": feedback, "revisionSourceJobId": source.id,
        "artifactProvenance": deepcopy(source.result_json.get("artifactProvenance", {}))}
    job = LearningJob(id=str(uuid4()), task_id=source.task_id, kind="distill",
        fingerprint=fingerprint, status="queued", attempts=0, result_json=result)
    session.add(job)
    await session.flush()
    return job
