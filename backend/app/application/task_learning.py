"""Asynchronous, evidence-gated learning. Successful reuse never enters this queue."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import or_, select, update

from app.application.compiler import WorkflowCompiler, digest_json
from app.application.learning_contracts import ContractExample as ContractExample
from app.application.learning_contracts import InstructionSkillProposal as InstructionSkillProposal
from app.application.learning_contracts import LearningProposal as LearningProposal
from app.application.learning_contracts import SkillProposal as SkillProposal
from app.application.learning_contracts import SkillXProposal as SkillXProposal
from app.application.runtime_profiles import restore_agent_snapshot
from app.application.tasks import emit, task_catalog
from app.config import get_settings
from app.infrastructure.database import SessionLocal, Workflow, WorkflowVersion, utcnow
from app.infrastructure.models.tasks import Invocation, LearningJob, Task
from app.runtime.skillx import load_manifest
from app.runtime.skillx_host import execute_in_subprocess


def validate_learning_scope(proposal: LearningProposal, evidence: dict, kind: str) -> None:
    if proposal.decision == "skip":
        return
    if not proposal.skills and not proposal.skillx and not proposal.workflow:
        raise ValueError("没有可复用产物，应选择 skip")
    for skill in proposal.skills:
        if any(
            id not in evidence or evidence[id].status != "succeeded"
            for id in skill.sourceInvocationIds
        ):
            raise ValueError("Skill 缺少所引用的成功执行证据")
    if kind == "repair" and not proposal.workflow:
        raise ValueError("工作流修复必须提供修订契约")
    if proposal.workflow and kind != "repair":
        if not proposal.workflowScope.strip() or not proposal.workflowSourceInvocationIds:
            raise ValueError("工作流必须说明可复用片段的边界及来源调用")
        if any(
            id not in evidence or evidence[id].status != "succeeded"
            for id in proposal.workflowSourceInvocationIds
        ):
            raise ValueError("工作流片段引用了不存在或未成功的调用")


async def save_learned_skills(
    session, proposal: LearningProposal, roots: dict, registered: dict, job_id: str
) -> dict:
    from app.application.managed_skills import parse_skill, save_skill
    from app.infrastructure.models import CapabilityVersion, ManagedSkill

    raw_ids, converted_ids = {}, {}
    for skill in proposal.skills:
        metadata, body = parse_skill(skill.content)
        metadata.update(
            {
                "name": skill.name,
                "description": skill.description,
                "useWhen": skill.useWhen,
                "x-learning-job-id": job_id,
                "x-source-invocations": skill.sourceInvocationIds,
            }
        )
        content = "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n\n" + body
        # Do not overwrite a user's existing guide; identical guides can be reused.
        candidates = await session.scalars(
            select(ManagedSkill).where(
                ManagedSkill.name == skill.name,
                ManagedSkill.description == skill.description,
                ManagedSkill.deleted_at.is_(None),
            )
        )
        existing = next(
            (
                candidate
                for candidate in candidates
                if parse_skill(candidate.content)[1] == body
                and parse_skill(candidate.content)[0].get("useWhen") == skill.useWhen
            ),
            None,
        )
        managed = existing or await save_skill(
            session,
            {
                "name": skill.name,
                "displayName": skill.displayName or skill.name,
                "description": skill.description,
                "content": content,
                "enabled": True,
            },
        )
        raw_ids[skill.name] = managed.id
    for skill in proposal.skillx:
        version = await session.get(CapabilityVersion, registered[skill.name])
        managed = await session.scalar(
            select(ManagedSkill).where(
                ManagedSkill.skillx_capability_id == version.capability_id,
                ManagedSkill.deleted_at.is_(None),
            )
        )
        if managed:
            raise ValueError("该 SkillX 已有受管理的版本，请复用其契约，避免覆盖现有 Skill")
        root = roots[skill.name]
        managed = await save_skill(
            session,
            {
                "name": skill.name,
                "displayName": skill.name,
                "description": skill.description,
                "content": (root / "SKILL.md").read_text(),
                "enabled": True,
            },
            imported=root,
        )
        managed.skillx_capability_id = version.capability_id
        managed.skillx_version_id = version.id
        managed.converted_source_digest = managed.source_digest
        converted_ids[skill.name] = managed.id
    return {"skills": raw_ids, "skillx": registered, "skillxSkills": converted_ids}


def observed_output(invocation):
    output = invocation.output_json
    if invocation.kind == "code":
        try:
            parsed = json.loads(output.get("stdout", ""))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return output


async def materialize_skill(proposal: SkillProposal, job_id: str, evidence: dict):
    """Only self-contained pure Python is promoted by this profile.

    Other dependency/effect profiles remain candidates until reproducible fixtures exist.
    """
    if proposal.kind != "direct":
        from app.application.model_materialization import materialize_model_skill

        return await materialize_model_skill(proposal, job_id, evidence)
    import ast

    from app.application.dependency_fixtures import fixture_handler, observed_dependencies

    async with SessionLocal() as session:
        dependency_locks, fixtures = await observed_dependencies(
            session, proposal.dependencies, evidence
        )
    tree = ast.parse(proposal.source)
    # Generated packages cannot silently gain network/filesystem/process dependencies.
    allowed_imports = {
        "math",
        "statistics",
        "decimal",
        "fractions",
        "re",
        "json",
        "datetime",
        "collections",
        "itertools",
        "functools",
        "operator",
        "string",
        "typing",
    }
    forbidden = {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "setattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
        ):
            if (
                len(node.args) < 2
                or not isinstance(node.args[1], ast.Constant)
                or not isinstance(node.args[1].value, str)
                or node.args[1].value.startswith("_")
            ):
                raise ValueError("getattr 只允许静态公开属性名称")
        if isinstance(node, ast.Import) and any(
            x.name.split(".")[0] not in allowed_imports for x in node.names
        ):
            raise ValueError("生成能力需要额外依赖验证，保留候选")
        if isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").split(".")[0] not in allowed_imports
        ):
            raise ValueError("生成能力需要额外依赖验证，保留候选")
        if isinstance(node, ast.Name) and (node.id in forbidden or node.id.startswith("__")):
            raise ValueError("生成能力包含需要人工授权的动态执行或 I/O")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("生成能力不允许访问内部属性")
    verified = [
        example
        for example in proposal.examples
        if example.sourceInvocationId in evidence
        and evidence[example.sourceInvocationId].status == "succeeded"
        and observed_output(evidence[example.sourceInvocationId]) == example.expected
    ]
    if not verified or len({digest_json(x.input) for x in proposal.examples}) < 2:
        raise ValueError("缺少原任务结果证据或不同输入测试")
    root = get_settings().capability_root.resolve() / "generated" / job_id / proposal.name
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {"name": proposal.name, "description": proposal.description}, allow_unicode=True
        )
        + f"---\n{proposal.description}\n"
    )
    (root / "input.json").write_text(json.dumps(proposal.inputSchema))
    (root / "output.json").write_text(json.dumps(proposal.outputSchema))
    (root / "execute.py").write_text(proposal.source)
    async with SessionLocal() as session:
        from app.infrastructure.database import Capability, CapabilityVersion

        previous = await session.scalar(
            select(CapabilityVersion)
            .join(Capability)
            .where(Capability.name == proposal.name)
            .order_by(CapabilityVersion.created_at.desc())
            .limit(1)
        )
        version_number = "0.1.0"
        if previous:
            major, minor, patch = previous.version.split(".")
            version_number = f"{major}.{minor}.{int(patch) + 1}"
    manifest = {
        "apiVersion": "skillx.dev/v1alpha1",
        "kind": "SkillX",
        "metadata": {
            "name": proposal.name,
            "version": version_number,
            "description": proposal.description,
        },
        "spec": {
            "guide": {"entry": "SKILL.md"},
            "defaultInterface": "execute",
            "capabilities": proposal.dependencies,
            "interfaces": {
                "execute": {
                    "kind": "direct",
                    "description": proposal.description,
                    "inputSchema": "input.json",
                    "outputSchema": "output.json",
                    "runtime": {
                        "protocol": "skillx.python-callable/v1",
                        "entry": "execute.py:execute",
                    },
                    "execution": {"timeoutSeconds": 30, "idempotency": "inherent"},
                    "effects": [],
                    "capabilities": list(proposal.dependencies),
                }
            },
        },
    }
    (root / "skillx.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True))
    load_manifest(root)
    import asyncio

    for index, example in enumerate(proposal.examples):
        Draft202012Validator(proposal.inputSchema).validate(example.input)
        async with asyncio.timeout(30):
            result = await execute_in_subprocess(
                root,
                "execute",
                f"{job_id}-{index}",
                example.input,
                dependency_handler=fixture_handler(fixtures),
            )
        if result.status != "succeeded" or result.output != example.expected:
            raise ValueError(
                f"{proposal.name} 契约测试 {index} 不通过: {result.error or result.output}"
            )
    from app.application.clean_validation import validate_clean_package

    await validate_clean_package(root, proposal.examples, proposal.outputSchema, fixtures=fixtures)
    (root / "dependency-validation.json").write_text(
        json.dumps(
            {
                "locks": dependency_locks,
                "fixtures": fixtures,
            }
        )
    )
    (root / "tests.json").write_text(json.dumps([x.model_dump() for x in proposal.examples]))
    return root


class LearningDriver:
    async def discover_failures(self):
        from uuid import NAMESPACE_URL, uuid5

        from app.application.workflow_patch import PATCHABLE_EVIDENCE_KINDS
        from app.infrastructure.database import FailureObservation, Run

        async with SessionLocal() as session, session.begin():
            observations = await session.scalars(
                select(FailureObservation)
                .where(
                    FailureObservation.workflow_version_id.is_not(None),
                    FailureObservation.evidence_kind.in_(PATCHABLE_EVIDENCE_KINDS),
                )
                .order_by(FailureObservation.updated_at.desc())
                .limit(100)
            )
            for observation in observations:
                run = await session.get(Run, observation.last_run_id)
                if not run or run.status not in {"succeeded", "failed"}:
                    continue
                call = await session.scalar(select(Invocation).where(Invocation.run_id == run.id))
                if call and call.task_id:
                    owner = await session.get(Task, call.task_id)
                    if owner and owner.status not in {
                        "succeeded",
                        "failed",
                        "blocked",
                        "cancelled",
                        "manual",
                    }:
                        continue
                task_id = (
                    call.task_id
                    if call and call.task_id
                    else str(uuid5(NAMESPACE_URL, observation.fingerprint))
                )
                fingerprint = digest_json(
                    {"version": observation.workflow_version_id, "failure": observation.fingerprint}
                )
                if await session.scalar(
                    select(LearningJob.id).where(LearningJob.fingerprint == fingerprint)
                ):
                    continue
                if not await session.get(Task, task_id):
                    from app.application.runtime_profiles import agent_snapshot

                    session.add(
                        Task(
                            id=task_id,
                            goal="修复工作流失败：" + observation.error_code,
                            status="manual",
                            state_json={},
                            runtime_json=await agent_snapshot(session, None),
                        )
                    )
                    await session.flush()
                    session.add(
                        Invocation(
                            id=str(uuid4()),
                            task_id=task_id,
                            scope=task_id,
                            call_key="failure",
                            kind="workflow",
                            status=run.status,
                            request_json={
                                "versionId": run.workflow_version_id,
                                "input": run.input_json,
                            },
                            output_json=run.output_json or {},
                            error_json=run.error_json or {"code": observation.error_code},
                            run_id=run.id,
                        )
                    )
                session.add(
                    LearningJob(
                        id=str(uuid4()),
                        task_id=task_id,
                        kind="repair",
                        fingerprint=fingerprint,
                        result_json={"observationId": observation.id},
                    )
                )

    async def poll_once(self):
        await self.discover_failures()
        async with SessionLocal() as session, session.begin():
            job = await session.scalar(
                select(LearningJob)
                .where(
                    LearningJob.status.in_(["queued", "running"]),
                    LearningJob.attempts < 3,
                    or_(
                        LearningJob.lease_expires_at.is_(None),
                        LearningJob.lease_expires_at < utcnow(),
                    ),
                )
                .order_by(LearningJob.created_at)
                .limit(1)
            )
            if job is None:
                return False
            published = await session.scalar(
                select(WorkflowVersion).where(
                    WorkflowVersion.definition_json["metadata"]["learningJobId"].as_string()
                    == job.id
                )
            )
            if published:
                job.status = "published"
                job.result_json = {
                    "workflowVersionId": published.id,
                    **published.definition_json.get("metadata", {}).get("learningArtifacts", {}),
                    "reason": "从已提交的发布证据恢复",
                }
                job.lease_token, job.lease_expires_at = None, None
                return True
            token = str(uuid4())
            changed = await session.execute(
                update(LearningJob)
                .where(
                    LearningJob.id == job.id,
                    LearningJob.attempts == job.attempts,
                    or_(
                        LearningJob.lease_expires_at.is_(None),
                        LearningJob.lease_expires_at < utcnow(),
                    ),
                )
                .values(
                    status="running",
                    lease_token=token,
                    attempts=LearningJob.attempts + 1,
                    lease_expires_at=utcnow() + timedelta(minutes=10),
                )
            )
            if changed.rowcount != 1:
                return False
            task = await session.get(Task, job.task_id)
            calls = list(
                await session.scalars(select(Invocation).where(Invocation.task_id == task.id))
            )
            evidence = {x.id: x for x in calls}
            job_id, task_id, kind = job.id, task.id, job.kind
            attempts = job.attempts
            observation_id = job.result_json.get("observationId")
            from pathlib import Path

            from app.domain.workflow import WorkflowDefinition

            context = {
                "workflowSchema": WorkflowDefinition.model_json_schema(),
                "workflowExample": json.loads(
                    (
                        Path(__file__).resolve().parents[3] / "examples/greeting-workflow.json"
                    ).read_text()
                ),
                "previousAttempt": job.result_json,
                "goal": task.goal,
                "kind": kind,
                "finalState": task.state_json,
                "outcome": {
                    "status": task.status,
                    "output": task.output_json,
                    "error": task.error_json,
                },
                "catalog": await task_catalog(session),
                "calls": [
                    {
                        "id": x.id,
                        "kind": x.kind,
                        "status": x.status,
                        "input": x.request_json,
                        "output": x.output_json,
                        "error": x.error_json,
                    }
                    for x in calls
                ],
            }
            from app.infrastructure.database import Message
            from app.infrastructure.models.tasks import HumanInteraction, TaskEvent

            context["sessionMessages"] = (
                [
                    {"role": row.role, "content": row.content_json}
                    for row in await session.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == task.conversation_id,
                            Message.created_at <= task.updated_at,
                        )
                        .order_by(Message.created_at)
                    )
                ]
                if task.conversation_id
                else []
            )
            context["humanAnswers"] = [
                {"kind": row.kind, "request": row.request_json, "answer": row.answer_json}
                for row in await session.scalars(
                    select(HumanInteraction)
                    .where(HumanInteraction.task_id == task.id)
                    .order_by(HumanInteraction.created_at)
                )
            ]
            context["decisions"] = [
                row.payload_json
                for row in await session.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task.id, TaskEvent.type == "task.decision")
                    .order_by(TaskEvent.id)
                )
            ]
            if observation_id:
                from app.infrastructure.database import FailureObservation, NodeRun

                failure = await session.get(FailureObservation, observation_id)
                failed_node = (
                    await session.get(NodeRun, failure.source_node_run_id)
                    if failure and failure.source_node_run_id
                    else None
                )
                context["failure"] = {
                    "workflowVersionId": failure.workflow_version_id if failure else None,
                    "nodeId": failure.node_id if failure else None,
                    "input": failed_node.input_json if failed_node else None,
                    "error": failed_node.error_json if failed_node else None,
                    "recoveredOutcome": failure.recovered_outcome_json if failure else None,
                }
            runtime = restore_agent_snapshot(task.runtime_json)
            from app.application.model_materialization import model_runtime

            available_kinds = ["direct", "agent"]
            llm_runtime = await model_runtime(session, "llm")
            if llm_runtime.get("modelSnapshot"):
                available_kinds.append("llm")
            context["availableSkillKinds"] = available_kinds
            from app.infrastructure.models import ManagedSkill

            context["existingSkills"] = [
                {"id": skill.id, "name": skill.name, "description": skill.description}
                for skill in await session.scalars(
                    select(ManagedSkill).where(
                        ManagedSkill.enabled.is_(True), ManagedSkill.deleted_at.is_(None)
                    )
                )
            ]
        proposal = None
        if kind == "repair":
            from app.application.automatic_repair import revise_from_failure
            from app.application.runtime_config import agent_runtime_scope

            try:
                async with SessionLocal() as session:
                    from app.infrastructure.database import FailureObservation

                    versions = {
                        c.request_json.get("versionId") for c in calls if c.kind == "workflow"
                    }
                    observation = await session.scalar(
                        select(FailureObservation).where(
                            FailureObservation.id == observation_id
                            if observation_id
                            else FailureObservation.workflow_version_id.in_(versions)
                        )
                    )
                if observation and observation.node_type != "skillx":
                    with agent_runtime_scope(runtime):
                        repaired = await revise_from_failure(
                            observation.workflow_version_id, observation.id
                        )
                    async with SessionLocal() as session, session.begin():
                        current = await session.get(LearningJob, job_id)
                        if current.lease_token == token:
                            current.status, current.result_json = "published", repaired
                            current.lease_token, current.lease_expires_at = None, None
                            emit(session, task_id, "learning.published", repaired)
                    return True
            except Exception as exc:
                async with SessionLocal() as session, session.begin():
                    current = await session.get(LearningJob, job_id)
                    if current.lease_token == token:
                        current.status = "blocked"
                        current.result_json = {"reason": str(getattr(exc, "detail", exc))}
                        current.lease_token, current.lease_expires_at = None, None
                return True
        try:
            from app.application.learning_agents import LearningAgentPipeline

            pipeline = LearningAgentPipeline(job_id, task_id, token, runtime)
            proposal = await pipeline.generate(
                context,
                materializer=lambda skill, source_ids: materialize_skill(
                    skill, job_id, {id: evidence[id] for id in source_ids}
                ),
            )
            if any(skill.kind not in available_kinds for skill in proposal.skillx):
                raise ValueError("候选使用了当前未配置的模型接口")
            validate_learning_scope(proposal, evidence, kind)
            if proposal.decision == "skip":
                status, output = "skipped", {"reason": proposal.reason}
            else:
                roots = pipeline.materialized_roots
                async with SessionLocal() as session:
                    await pipeline._owned_job(session)
                    from copy import deepcopy

                    from app.api.schemas import PublishRequest
                    from app.application.capabilities import register_skillx
                    from app.application.workflow_publishing import publish_workflow

                    registered = {}
                    for name, root in roots.items():
                        _, version = await register_skillx(
                            session, str(root.relative_to(get_settings().capability_root.resolve()))
                        )
                        registered[name] = version.id
                    artifacts = await save_learned_skills(
                        session, proposal, roots, registered, job_id
                    )
                    if not proposal.workflow:
                        # Commit standalone capabilities and job completion together.
                        current = await session.get(LearningJob, job_id)
                        if current.lease_token != token:
                            raise ValueError("固化任务租约已失效")
                        output = {
                            **artifacts,
                            "reason": proposal.reason,
                            "agentStages": current.result_json.get("agentStages", {}),
                        }
                        current.status, current.result_json = "published", output
                        current.lease_token, current.lease_expires_at = None, None
                        emit(
                            session,
                            task_id,
                            "learning.published",
                            {"jobId": job_id, "kind": kind, **output},
                        )
                        await session.commit()
                        return True
                    definition = deepcopy(proposal.workflow)
                    definition.setdefault("metadata", {}).update(
                        {
                            "learningJobId": job_id,
                            "learningArtifacts": artifacts,
                            "reuseScope": proposal.workflowScope,
                            "sourceInvocationIds": proposal.workflowSourceInvocationIds,
                        }
                    )
                    for node in definition["spec"]["nodes"]:
                        name = node.get("config", {}).pop("generatedSkill", None)
                        if name:
                            node["config"]["capabilityVersionId"] = registered[name]
                    # A model cannot auto-publish untested side effects or arbitrary nodes.
                    if any(
                        n["type"]
                        not in {
                            "input",
                            "output",
                            "skillx",
                            "condition",
                            "transform",
                            "capability_call",
                            "workflow_call",
                        }
                        for n in definition["spec"]["nodes"]
                    ):
                        raise ValueError("工作流包含尚未提供回放证据的节点，保留候选")
                    compiled = WorkflowCompiler().compile(definition)
                    if kind == "repair":
                        from app.application.workflow_patch import PATCHABLE_EVIDENCE_KINDS
                        from app.infrastructure.database import FailureObservation

                        failed_versions = {
                            x.request_json.get("versionId") for x in calls if x.kind == "workflow"
                        }
                        if proposal.baseVersionId not in failed_versions:
                            raise ValueError("修订基版本不属于本次故障")
                        observation = await session.scalar(
                            select(FailureObservation).where(
                                FailureObservation.workflow_version_id == proposal.baseVersionId,
                                FailureObservation.id == observation_id,
                            )
                        )
                        if (
                            not observation
                            or observation.evidence_kind not in PATCHABLE_EVIDENCE_KINDS
                        ):
                            raise ValueError("瞬态或未知故障不允许自动修订")
                        base = await session.get(WorkflowVersion, proposal.baseVersionId)
                        from app.application.learning_permissions import verify_repair_authorization
                        from app.application.workflow_nesting import expand_workflow

                        expanded = await expand_workflow(session, definition)
                        await verify_repair_authorization(session, base.definition_json, expanded)
                        workflow = await session.get(Workflow, base.workflow_id)
                        if (
                            WorkflowCompiler().compile(workflow.draft_json).digest
                            != base.published_from_draft_digest
                        ):
                            raise ValueError("工作流存在其他草稿，保留修订候选")
                        old_ids = {
                            n.get("config", {}).get("capabilityVersionId")
                            for n in base.definition_json["spec"]["nodes"]
                        }
                        if any(
                            n["type"] == "skillx"
                            and n["config"]["capabilityVersionId"]
                            not in old_ids | set(registered.values())
                            for n in expanded["spec"]["nodes"]
                        ):
                            raise ValueError("修订不得引入未验证能力")
                        workflow.draft_revision += 1
                        workflow.draft_json, workflow.draft_digest = definition, compiled.digest
                        workflow.draft_metadata_json = {
                            "repairOf": base.id,
                            "learningJobId": job_id,
                        }
                    else:
                        workflow = Workflow(
                            id=str(uuid4()),
                            name=definition["metadata"]["name"],
                            draft_revision=1,
                            draft_json=definition,
                            draft_digest=compiled.digest,
                        )
                        session.add(workflow)
                    await session.flush()
                    # Compile and package tests are insufficient evidence for full graph behavior.
                    from app.application.learning_validation import validate_candidate

                    await validate_candidate(
                        session,
                        definition,
                        proposal.workflowExamples,
                        evidence,
                        generated_ids=set(registered.values()),
                    )
                    await pipeline._owned_job(session)
                    published = await publish_workflow(
                        workflow.id,
                        PublishRequest(
                            draftRevision=workflow.draft_revision, draftDigest=compiled.digest
                        ),
                        session,
                    )
                    output = {
                        "workflowVersionId": published["id"],
                        **artifacts,
                        "scope": proposal.workflowScope,
                        "reason": proposal.reason,
                    }
                    status = "published"
        except Exception as exc:
            diagnostics = getattr(exc, "diagnostics", None)
            reason = str(exc)
            if diagnostics:
                reason += ": " + "; ".join(f"{d.path}: {d.message}" for d in diagnostics)
            if proposal and proposal.workflow and pipeline.workflow_name:
                from app.application.learning_agents import LearningLeaseLost

                try:
                    await pipeline.invalidate("workflow", pipeline.workflow_name, reason)
                except LearningLeaseLost:
                    pass
            status, output = (
                "queued" if attempts < 3 else "blocked",
                {"reason": reason, "proposal": proposal.model_dump() if proposal else None},
            )
        async with SessionLocal() as session, session.begin():
            current = await session.get(LearningJob, job_id)
            if current.lease_token == token:
                if observation_id:
                    output["observationId"] = observation_id
                output["agentStages"] = current.result_json.get("agentStages", {})
                current.status, current.result_json = status, output
                current.lease_token, current.lease_expires_at = None, None
                emit(
                    session,
                    task_id,
                    "learning." + status,
                    {"jobId": job_id, "kind": kind, **output},
                )
        return True
