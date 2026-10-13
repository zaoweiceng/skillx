"""Publish independently validated artifacts; dependencies never undo their siblings."""
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select

from app.application.compiler import WorkflowCompiler
from app.application.learning_revisions import RevisionConflict
from app.application.learning_contracts import LearningProposal
from app.application.learning_validation_budget import validation_budget
from app.infrastructure.database import SessionLocal, Workflow, WorkflowVersion


async def publish_artifacts(pipeline, proposal, evidence):
    from app.api.schemas import PublishRequest
    from app.application.capabilities import register_skillx
    from app.application.learning_agents import LearningLeaseLost
    from app.application.learning_validation import validate_candidate
    from app.application.task_learning import save_learned_skills, validate_learning_scope
    from app.application.workflow_publishing import publish_workflow
    from app.config import get_settings

    async with SessionLocal() as session:
        job = await pipeline._owned_job(session)
        artifacts = deepcopy(job.result_json.get("artifacts", {}))
    for key in ("skills", "skillx", "skillxSkills", "workflows"):
        artifacts.setdefault(key, {})
    failures = dict(proposal.failures)

    async def save(session):
        job = await pipeline._owned_job(session)
        job.result_json = {**job.result_json, "artifacts": deepcopy(artifacts)}
        await session.commit()

    for skill in [*proposal.skills, *proposal.skillx]:
        is_script = skill in proposal.skillx
        group = "skillx" if is_script else "skills"
        if skill.name in artifacts[group]:
            continue
        try:
            async with SessionLocal() as session:
                await pipeline._owned_job(session)
                isolated = LearningProposal(decision="candidate", reason=proposal.reason,
                    **{group: [skill]})
                validate_learning_scope(isolated, evidence, "distill")
                registered, roots = {}, {}
                if is_script:
                    root = pipeline.materialized_roots[skill.name]
                    _, version = await register_skillx(session, str(root.relative_to(get_settings().capability_root.resolve())))
                    registered[skill.name], roots[skill.name] = version.id, root
                result = await save_learned_skills(session, isolated, roots, registered, pipeline.job_id)
                before = deepcopy(artifacts)
                for key, values in result.items():
                    artifacts[key].update(values)
                try:
                    await save(session)
                except Exception:
                    artifacts = before
                    raise
        except LearningLeaseLost:
            raise
        except Exception as exc:
            failures[group + ":" + skill.name] = str(exc)

    for flow in proposal.workflows:
        if flow.name in artifacts["workflows"]:
            continue
        missing = set(flow.dependsOn) - (artifacts["skillx"].keys() | artifacts["workflows"].keys())
        if missing:
            failures["workflow:" + flow.name] = "依赖未发布：" + "、".join(sorted(missing))
            continue
        try:
            async with SessionLocal() as session:
                await pipeline._owned_job(session)
                if not flow.scope.strip() or not flow.sourceInvocationIds or any(
                    id not in evidence or evidence[id].status != "succeeded" for id in flow.sourceInvocationIds
                ):
                    raise ValueError("工作流缺少明确边界或成功调用证据")
                definition = deepcopy(flow.definition)
                from app.domain.capability_metadata import validate_publication_metadata
                validate_publication_metadata(definition.get("metadata", {}))
                definition.setdefault("metadata", {}).update(
                    learningJobId=pipeline.job_id, learningArtifactName=flow.name,
                    reuseScope=flow.scope, sourceInvocationIds=flow.sourceInvocationIds,
                )
                for node in definition["spec"]["nodes"]:
                    config = node.setdefault("config", {})
                    name = config.pop("generatedSkill", None)
                    if name:
                        if name not in flow.dependsOn or name not in artifacts["skillx"]:
                            raise ValueError("引用未声明或未发布的 SkillX")
                        config["capabilityVersionId"] = artifacts["skillx"][name]
                    name = config.pop("generatedWorkflow", None)
                    if name:
                        if name not in flow.dependsOn or name not in artifacts["workflows"] or node["type"] != "workflow_call":
                            raise ValueError("引用未声明或未发布的子工作流")
                        version = await session.get(WorkflowVersion, artifacts["workflows"][name])
                        config.update(workflowVersionId=version.id, digest=version.digest)
                compiled = WorkflowCompiler().compile(definition)
                job = await pipeline._owned_job(session)
                base_id = job.result_json.get("revisionBaseWorkflowVersions", {}).get(flow.name)
                if base_id:
                    base = await session.get(WorkflowVersion, base_id)
                    workflow = await session.get(Workflow, base.workflow_id)
                    latest = await session.scalar(select(WorkflowVersion).where(
                        WorkflowVersion.workflow_id == workflow.id).order_by(WorkflowVersion.version.desc()).limit(1))
                    if (latest.id != base_id or workflow.draft_digest not in {base.digest, base.published_from_draft_digest}
                        or workflow.draft_revision != base.published_from_draft_revision):
                        raise RevisionConflict("工作流基线已变化，禁止覆盖其他修订")
                with validation_budget(pipeline.job_id,
                        lambda request_id, metrics: pipeline.record_validation_usage(flow.name, request_id, metrics)):
                    await validate_candidate(session, definition, flow.examples,
                        {id: evidence[id] for id in flow.sourceInvocationIds},
                        generated_ids=set(artifacts["skillx"].values()))
                if base_id:
                    workflow.draft_revision += 1
                    workflow.draft_json, workflow.draft_digest = definition, compiled.digest
                    workflow.draft_base_version_id = base_id
                else:
                    workflow = Workflow(id=str(uuid4()), name=definition["metadata"]["name"],
                        draft_revision=1, draft_json=definition, draft_digest=compiled.digest)
                    session.add(workflow)
                await session.flush()
                result = await publish_workflow(workflow.id,
                    PublishRequest(draftRevision=workflow.draft_revision, draftDigest=compiled.digest), session, commit=False)
                artifacts["workflows"][flow.name] = result["id"]
                try:
                    await save(session)
                except Exception:
                    artifacts["workflows"].pop(flow.name, None)
                    raise
        except LearningLeaseLost:
            raise
        except Exception as exc:
            failure_key = "workflow:" + flow.name
            failures[failure_key] = str(exc)
            if isinstance(exc, (RevisionConflict, KeyError)):
                if isinstance(exc, KeyError):
                    from traceback import extract_tb
                    from pathlib import Path
                    frame = extract_tb(exc.__traceback__)[-1]
                    location = f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})"
                    failures[failure_key] = f"SYSTEM_CONTRACT_ERROR: {location} 后端发布链读取缺失字段 {exc}，需要修复系统契约，未消耗 Agent 修订"
                continue
            # Compilation/execution feedback belongs to this builder only.
            # Its siblings were committed separately and remain available.
            try:
                revised = await pipeline.revise_workflow(flow, exc)
                if revised is not None:
                    _, corrected = await publish_artifacts(pipeline,
                        LearningProposal(decision="candidate", reason=proposal.reason, workflows=[revised]), evidence)
                    artifacts = corrected["artifacts"]
                    if failure_key in corrected["failures"]:
                        failures[failure_key] = corrected["failures"][failure_key]
                    elif flow.name in artifacts["workflows"]:
                        failures.pop(failure_key, None)
            except LearningLeaseLost:
                raise
            except Exception as revision_error:
                failures[failure_key] = str(revision_error)
    async with SessionLocal() as session:
        artifacts = deepcopy((await pipeline._owned_job(session)).result_json.get("artifacts", artifacts))
    count = sum(len(artifacts[key]) for key in ("skills", "skillx", "workflows"))
    status = "partial" if count and failures else "published" if count else "blocked" if failures else "skipped"
    return status, {**artifacts, "artifacts": artifacts, "failures": failures, "reason": proposal.reason}
