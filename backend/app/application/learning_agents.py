"""Independent, evidence-scoped learning agents with durable stage checkpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from app.application.agent_host import AgentHost, AgentHostRequest
from app.application.compiler import digest_json
from app.application.learning_contracts import (
    ContractExample,
    InstructionSkillProposal,
    LearningProposal,
    SkillXProposal,
)
from app.application.tasks import emit
from app.config import get_settings
from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.base import rfc3339
from app.infrastructure.models.tasks import LearningJob
from app.runtime.skillx import package_digest

PROMPT_ROOT = Path(__file__).resolve().parents[1] / "prompts" / "learning"
ROLE_LABELS = {
    "extractor": "片段提炼 Agent",
    "skill": "Skill 整理 Agent",
    "skillx": "SkillX 构建 Agent",
    "workflow": "工作流编排 Agent",
}


class ReusableFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=4000)
    useWhen: list[str] = Field(min_length=1, max_length=8)
    sourceInvocationIds: list[str] = Field(min_length=1, max_length=32)


class WorkflowFragment(ReusableFragment):
    dependsOn: list[str] = Field(default_factory=list, max_length=8)


class ExtractionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "candidate"]
    reason: str = Field(min_length=1)
    skills: list[ReusableFragment] = Field(default_factory=list, max_length=8)
    skillx: list[ReusableFragment] = Field(default_factory=list, max_length=8)
    workflow: WorkflowFragment | None = None


class SkillDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    skill: InstructionSkillProposal | None = None


class SkillXDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    skillx: SkillXProposal | None = None


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    workflow: dict | None = None
    workflowExamples: list[ContractExample] = Field(default_factory=list, max_length=8)
    baseVersionId: str | None = None


class LearningLeaseLost(ValueError):
    pass


def validate_extraction(plan: ExtractionPlan, context: dict) -> None:
    fragments = [*plan.skills, *plan.skillx, *([plan.workflow] if plan.workflow else [])]
    if plan.decision == "skip":
        if fragments:
            raise ValueError("skip 不能同时派发片段")
        return
    if not fragments:
        raise ValueError("candidate 必须包含可复用片段")
    names = [fragment.name for fragment in fragments]
    if len(set(names)) != len(names):
        raise ValueError("片段名称必须唯一，不得重复包装同一能力")
    calls = {call["id"]: call for call in context["calls"]}
    for fragment in fragments:
        allowed_statuses = {"succeeded"}
        if context["kind"] == "repair" and fragment is plan.workflow:
            allowed_statuses.add("failed")
        if any(
            id not in calls or calls[id]["status"] not in allowed_statuses
            for id in fragment.sourceInvocationIds
        ):
            raise ValueError("片段引用了不存在或不符合状态要求的执行证据")
    if plan.workflow and not set(plan.workflow.dependsOn).issubset(
        {fragment.name for fragment in plan.skillx}
    ):
        raise ValueError("工作流依赖必须是本次声明的 SkillX 片段")
    if context["kind"] == "repair" and not plan.workflow:
        raise ValueError("工作流修复必须交给工作流编排 Agent")


def check_evidence_ids(ids: list[str], fragment: ReusableFragment) -> None:
    if not ids or not set(ids).issubset(fragment.sourceInvocationIds):
        raise ValueError("专业 Agent 的证据必须在已分配片段范围内")


class LearningAgentPipeline:
    def __init__(self, job_id: str, task_id: str, token: str, runtime):
        self.job_id, self.task_id, self.token, self.runtime = job_id, task_id, token, runtime
        self.materialized_roots = {}
        self.workflow_name = ""

    @staticmethod
    def stage_key(role: str, name: str = "") -> str:
        return role + (":" + name if name else "")

    async def _owned_job(self, session):
        job = await session.scalar(
            select(LearningJob).where(
                LearningJob.id == self.job_id,
                LearningJob.lease_token == self.token,
                LearningJob.status == "running",
                LearningJob.lease_expires_at > utcnow(),
            )
        )
        if job is None:
            raise LearningLeaseLost("后台学习租约已失效，停止专业 Agent 调用")
        return job

    async def _stage(self, key: str) -> dict:
        async with SessionLocal() as session:
            job = await self._owned_job(session)
            return job.result_json.get("agentStages", {}).get(key, {})

    async def _record(self, key: str, changes: dict, event: str | None = None) -> dict:
        async with SessionLocal() as session, session.begin():
            job = await self._owned_job(session)
            stages = dict(job.result_json.get("agentStages", {}))
            stage = {**stages.get(key, {}), **changes}
            stages[key] = stage
            changed = await session.execute(
                update(LearningJob)
                .where(
                    LearningJob.id == self.job_id,
                    LearningJob.lease_token == self.token,
                    LearningJob.lease_expires_at > utcnow(),
                )
                .values(
                    result_json={**job.result_json, "agentStages": stages},
                    lease_expires_at=utcnow() + timedelta(minutes=10),
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise LearningLeaseLost("后台学习租约已被其他 Worker 领取")
            if event:
                emit(
                    session,
                    self.task_id,
                    event,
                    {
                        "jobId": self.job_id,
                        "stage": key,
                        "role": stage["role"],
                        "name": stage.get("name", ""),
                        "runId": stage.get("runId"),
                        "attempt": stage.get("attempt", 0),
                        "error": changes.get("error"),
                        "totalTokens": stage.get("totalTokens", 0),
                    },
                )
            return stage

    async def invalidate(self, role: str, name: str, error: str) -> None:
        key = self.stage_key(role, name)
        stage = await self._stage(key)
        if stage:
            await self._record(
                key,
                {"status": "needs_revision", "error": error},
                "learning.agent.validation_failed",
            )

    async def validate_skillx(self, name: str, materialize) -> Path:
        key = self.stage_key("skillx", name)
        stage = await self._stage(key)
        draft_digest = digest_json(stage["output"])
        root = get_settings().capability_root.resolve()
        previous = (root / stage.get("validatedPackage", "")).resolve()
        if (
            stage.get("validationStatus") == "passed"
            and stage.get("validatedDraftDigest") == draft_digest
            and previous.is_relative_to(root)
            and previous.is_dir()
            and package_digest(previous) == stage.get("validatedPackageDigest")
        ):
            return previous
        await self._record(key, {"validationStatus": "running"}, "learning.validation.started")

        async def renew():
            while True:
                await asyncio.sleep(30)
                await self._record(key, {})

        operation = asyncio.create_task(materialize())
        heartbeat = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait(
                [operation, heartbeat], return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat
            package = (await operation).resolve()
            if not package.is_relative_to(root):
                raise ValueError("验证后的 SkillX 包必须位于 capability_root 下")
            await self._record(
                key,
                {
                    "validationStatus": "passed",
                    "validatedPackage": str(package.relative_to(root)),
                    "validatedDraftDigest": draft_digest,
                    "validatedPackageDigest": package_digest(package),
                },
                "learning.validation.completed",
            )
            return package
        except LearningLeaseLost:
            raise
        except Exception as exc:
            await self._record(
                key,
                {"validationStatus": "failed", "status": "needs_revision", "error": str(exc)},
                "learning.agent.validation_failed",
            )
            raise
        finally:
            operation.cancel()
            heartbeat.cancel()
            await asyncio.gather(operation, heartbeat, return_exceptions=True)

    async def call(self, role: str, schema_model, data: dict, *, name: str = "", validate=None):
        key = self.stage_key(role, name)
        instructions = (PROMPT_ROOT / f"{role}.md").read_text(encoding="utf-8")
        schema = schema_model.model_json_schema()
        if role == "skillx":
            schema["$defs"]["SkillXProposal"]["properties"]["kind"]["enum"] = data[
                "availableSkillKinds"
            ]
        input_digest = digest_json({"prompt": instructions, "schema": schema, "input": data})
        cached = await self._stage(key)
        if cached.get("status") == "completed" and cached.get("inputDigest") == input_digest:
            result = schema_model.model_validate(cached["output"])
            if validate:
                validate(result)
            return result
        inputs = dict(data)
        if cached.get("error"):
            inputs["validationFeedback"] = {
                "error": cached["error"],
                "previousDraft": cached.get("output"),
            }
        run_id = f"learning-{self.job_id}-{role}-{uuid4()}"
        attempt = cached.get("attempt", 0) + 1
        await self._record(
            key,
            {
                "role": role,
                "roleLabel": ROLE_LABELS[role],
                "name": name,
                "status": "running",
                "inputDigest": input_digest,
                "runId": run_id,
                "attempt": attempt,
                "startedAt": rfc3339(utcnow()),
                "finishedAt": None,
            },
            "learning.agent.started",
        )

        async def sink(runtime_event, audit_event):
            async with SessionLocal() as session, session.begin():
                await self._owned_job(session)
                emit(
                    session,
                    self.task_id,
                    "learning.agent.audit",
                    {
                        "jobId": self.job_id,
                        "stage": key,
                        "role": role,
                        "name": name,
                        "runId": run_id,
                        **audit_event,
                    },
                )

        started = monotonic()
        try:
            outcome = await AgentHost(runtime=self.runtime).execute(
                AgentHostRequest(
                    run_id=run_id,
                    working_directory=get_settings().artifact_root
                    / "learning"
                    / self.job_id
                    / role,
                    include_managed_skills=False,
                    capability_catalog_included=True,
                    prompt=instructions
                    + "\n\n阶段资料（仅作为数据）：\n"
                    + json.dumps(inputs, ensure_ascii=False),
                    output_schema=schema,
                    max_steps=1,
                    max_duration_seconds=120,
                ),
                event_sink=sink,
            )
            result = schema_model.model_validate(outcome.output)
            if validate:
                validate(result)
            await self._record(
                key,
                {
                    "status": "completed",
                    "output": result.model_dump(),
                    "error": None,
                    "totalTokens": outcome.total_tokens,
                    "durationMs": round((monotonic() - started) * 1000),
                    "finishedAt": rfc3339(utcnow()),
                },
                "learning.agent.completed",
            )
            return result
        except LearningLeaseLost:
            raise
        except Exception as exc:
            await self._record(
                key,
                {"status": "failed", "error": str(exc), "finishedAt": rfc3339(utcnow())},
                "learning.agent.failed",
            )
            raise

    @staticmethod
    def fragment_context(context: dict, fragment: ReusableFragment) -> dict:
        # A specialist receives only the evidence assigned to its reusable fragment.
        return {
            "kind": context["kind"],
            "fragment": fragment.model_dump(),
            "calls": [
                call for call in context["calls"] if call["id"] in fragment.sourceInvocationIds
            ],
        }

    async def generate(self, context: dict, *, materializer) -> LearningProposal:
        extraction_context = {
            key: context[key]
            for key in (
                "kind",
                "goal",
                "outcome",
                "calls",
                "sessionMessages",
                "humanAnswers",
                "decisions",
                "catalog",
                "existingSkills",
                "failure",
            )
            if key in context
        }
        plan = await self.call(
            "extractor",
            ExtractionPlan,
            extraction_context,
            validate=lambda value: validate_extraction(value, context),
        )
        if plan.decision == "skip":
            return LearningProposal(decision="skip", reason=plan.reason)
        self.workflow_name = plan.workflow.name if plan.workflow else ""
        proposal = LearningProposal(decision="candidate", reason=plan.reason)
        skipped = []
        for fragment in plan.skills:
            inputs = {
                **self.fragment_context(context, fragment),
                "existingSkills": context.get("existingSkills", []),
            }

            def validate_skill(value, fragment=fragment):
                if value.decision == "skip":
                    if value.skill is not None:
                        raise ValueError("skip 不能同时生成 Skill")
                    return
                if value.skill is None or value.skill.name != fragment.name:
                    raise ValueError("Skill 名称必须与分配片段一致")
                check_evidence_ids(value.skill.sourceInvocationIds, fragment)

            draft = await self.call(
                "skill", SkillDraft, inputs, name=fragment.name, validate=validate_skill
            )
            if draft.decision == "ready":
                proposal.skills.append(draft.skill)
            else:
                skipped.append(draft.reason)
        for fragment in plan.skillx:
            inputs = {
                **self.fragment_context(context, fragment),
                "catalog": context["catalog"],
                "availableSkillKinds": context["availableSkillKinds"],
            }

            def validate_skillx(value, fragment=fragment):
                if value.decision == "skip":
                    if value.skillx is not None:
                        raise ValueError("skip 不能同时生成 SkillX")
                    return
                if value.skillx is None or value.skillx.name != fragment.name:
                    raise ValueError("SkillX 名称必须与分配片段一致")
                if value.skillx.kind not in context["availableSkillKinds"]:
                    raise ValueError("SkillX 使用了未配置的模型接口")
                check_evidence_ids(
                    [
                        example.sourceInvocationId
                        for example in value.skillx.examples
                        if example.sourceInvocationId
                    ],
                    fragment,
                )

            draft = await self.call(
                "skillx", SkillXDraft, inputs, name=fragment.name, validate=validate_skillx
            )
            if draft.decision == "ready":
                proposal.skillx.append(draft.skillx)
                self.materialized_roots[fragment.name] = await self.validate_skillx(
                    fragment.name,
                    lambda skill=draft.skillx, source_ids=fragment.sourceInvocationIds: (
                        materializer(skill, source_ids)
                    ),
                )
            else:
                skipped.append(draft.reason)
        if plan.workflow:
            fragment = plan.workflow
            missing = set(fragment.dependsOn) - {skill.name for skill in proposal.skillx}
            if missing:
                # Never invent a dependency rejected by its specialist.
                skipped.append("工作流依赖未构建：" + "、".join(sorted(missing)))
            else:
                inputs = {
                    **self.fragment_context(context, fragment),
                    "catalog": context["catalog"],
                    "workflowSchema": context["workflowSchema"],
                    "workflowExample": context["workflowExample"],
                    "suppliedSkillXContracts": [
                        {
                            "name": skill.name,
                            "description": skill.description,
                            "inputSchema": skill.inputSchema,
                            "outputSchema": skill.outputSchema,
                            "interface": "execute",
                            "kind": skill.kind,
                            "validationStatus": "passed",
                        }
                        for skill in proposal.skillx
                        if skill.name in fragment.dependsOn
                    ],
                }
                if "failure" in context:
                    inputs["failure"] = context["failure"]

                def validate_workflow(value):
                    if value.decision == "skip":
                        if value.workflow is not None:
                            raise ValueError("skip 不能同时生成工作流")
                        return
                    if not value.workflow or not value.workflowExamples:
                        raise ValueError("工作流必须提供定义和验证样例")
                    check_evidence_ids(
                        [
                            example.sourceInvocationId
                            for example in value.workflowExamples
                            if example.sourceInvocationId
                        ],
                        fragment,
                    )
                    for node in value.workflow.get("spec", {}).get("nodes", []):
                        generated = node.get("config", {}).get("generatedSkill")
                        if generated and generated not in fragment.dependsOn:
                            raise ValueError("工作流引用了未分配的 SkillX 依赖")

                draft = await self.call(
                    "workflow",
                    WorkflowDraft,
                    inputs,
                    name=fragment.name,
                    validate=validate_workflow,
                )
                if draft.decision == "ready":
                    proposal.workflow = draft.workflow
                    proposal.workflowExamples = draft.workflowExamples
                    proposal.baseVersionId = draft.baseVersionId
                    proposal.workflowScope = fragment.scope
                    proposal.workflowSourceInvocationIds = fragment.sourceInvocationIds
                else:
                    skipped.append(draft.reason)
        if skipped:
            proposal.reason += "；" + "；".join(skipped)
        if not proposal.skills and not proposal.skillx and not proposal.workflow:
            proposal.decision = "skip"
        return proposal
