"""Independent, evidence-scoped learning agents with durable stage checkpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, create_model, model_validator
from jsonschema.exceptions import ValidationError as OutputSchemaError
from sqlalchemy import select, update

from app.application.agent_host import (
    AgentHost,
    AgentHostError,
    AgentHostRequest,
    AgentOutputValidationError,
)
from app.application.compiler import digest_json
from app.application.learning_contracts import (
    ContractExample,
    SkillXExample,
    PythonSource,
    GeneratedBusinessSchema,
    InstructionSkillProposal,
    LearningProposal,
    SkillXProposal,
    DirectSkillXProposal,
    ModelSkillXProposal,
    WorkflowProposal,
)
from app.application.tasks import emit
from app.config import get_settings
from app.infrastructure.database import SessionLocal, utcnow
from app.infrastructure.models.base import rfc3339
from app.infrastructure.models.tasks import LearningJob
from app.runtime.skillx import package_digest
from app.domain.workflow import WorkflowDefinition

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
    reuseVersionId: str | None = Field(default=None, description="复用目录中已发布工作流的精确版本 ID；已有等价阶段必须复用，不重新生成。复用阶段仍可被 dependsOn 引用。")


class ExtractionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "candidate"]
    reason: str = Field(min_length=1)
    skills: list[ReusableFragment] = Field(default_factory=list, max_length=8)
    skillx: list[ReusableFragment] = Field(default_factory=list, max_length=8)
    workflows: list[WorkflowFragment] = Field(default_factory=list, max_length=16)
    workflow: WorkflowFragment | None = None


class ComposedExtractionPlan(ExtractionPlan):
    """Only for requests explicitly requiring independent and composed workflows."""
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [{
        "if": {"properties": {"decision": {"const": "candidate"}}},
        "then": {"properties": {"workflows": {"minItems": 3}}},
    }]})
    workflows: list[WorkflowFragment] = Field(default_factory=list, max_length=16,
        description="至少两个可独立调用的子工作流（已有阶段用 reuseVersionId 复用），以及通过 dependsOn 引用它们的上层组合工作流。不是要求新建三个工作流。")

    @model_validator(mode="after")
    def composition(self):
        if self.decision == "skip":
            return self
        names = {item.name for item in self.workflows}
        if len(self.workflows) < 3 or not any(len(set(item.dependsOn) & (names - {item.name})) >= 2 for item in self.workflows):
            raise ValueError("本次要求独立工作流和组合工作流：至少一个上层工作流须引用两个独立子工作流，不能只串联 SkillX")
        return self


class SkillDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    skill: InstructionSkillProposal | None = None


class SkillXDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    skillx: Annotated[DirectSkillXProposal | ModelSkillXProposal, Field(discriminator="kind")] | None = None


class SkillXImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: PythonSource
    examples: list[SkillXExample] = Field(min_length=2, max_length=8)


class SkillXRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executionProfile: Literal["pure-python", "local-command"] | None = None
    inputSchema: GeneratedBusinessSchema | None = None
    outputSchema: GeneratedBusinessSchema | None = None
    source: PythonSource | None = None
    examples: list[SkillXExample] | None = Field(default=None, min_length=2, max_length=8)


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodeConfigs: dict[str, dict]


from app.application.dependency_reuse_contract import ReusedDependency


class WorkflowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [{
        "if": {"properties": {"decision": {"const": "ready"}}},
        "then": {"required": ["workflow", "workflowExamples"], "properties": {
            "workflow": {"type": "object"}, "workflowExamples": {"minItems": 2}}},
    }]})
    decision: Literal["skip", "ready"]
    reason: str = Field(min_length=1)
    # Publish/store dictionaries, but derive both parsing and model-facing schema
    # from the exact workflow type used by the executor.
    workflow: Annotated[WorkflowDefinition,
        AfterValidator(lambda value: value.model_dump(mode="json", by_alias=True, exclude_unset=True)),
        PlainSerializer(lambda value: value)] | None = None
    workflowExamples: list[ContractExample] = Field(default_factory=list, max_length=8)
    reusedDependencies: dict[str, ReusedDependency] = Field(default_factory=dict)
    baseVersionId: str | None = None

    @model_validator(mode="after")
    def ready_artifact(self):
        if self.decision == "ready" and (not self.workflow or len(self.workflowExamples) < 2):
            raise ValueError("ready 工作流必须提供完整定义和至少两个验证样例")
        if self.decision == "ready":
            from app.domain.capability_metadata import validate_publication_metadata
            validate_publication_metadata(self.workflow.get("metadata", {}))
        return self


class WorkflowExamples(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflowExamples: list[ContractExample] = Field(min_length=2, max_length=8)


class DependencyRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodeId: str = Field(min_length=1)
    versionId: str = Field(min_length=1)
    feedback: str = Field(min_length=1)


class WorkflowRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopeChange: str | None = Field(default=None, pattern=r"\S", description="明确说明支持范围变化；同步元数据与输入Schema，不得静默删除声明形式")
    dependencyRepairs: list[DependencyRepairRequest] | None = Field(default=None, min_length=1, max_length=2)
    reusedDependencies: dict[str, ReusedDependency] | None = None
    workflow: WorkflowDefinition | None = None
    workflowExamples: list[ContractExample] | None = Field(default=None, min_length=2, max_length=8)


class WorkflowBindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodeInputs: dict[str, dict]


def missing_bindings(definition, inputs):
    from app.application.extraction_graph_contract import required_node_inputs
    missing = {}
    for node in definition["spec"]["nodes"]:
        required = required_node_inputs(node, inputs)
        if node["type"] == "output":
            required = definition["spec"]["outputSchema"].get("required", [])
        absent = set(required) - node.get("input", {}).keys()
        if absent:
            missing[node["id"]] = sorted(absent)
    return missing


class LearningLeaseLost(ValueError):
    pass


def validate_extraction(plan: ExtractionPlan, context: dict, *, check_dependencies: bool = True) -> None:
    workflows = [*plan.workflows, *([plan.workflow] if plan.workflow else [])]
    fragments = [*plan.skills, *plan.skillx, *workflows]
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
        if context["kind"] == "repair" and fragment in workflows:
            allowed_statuses.add("failed")
        if any(
            id not in calls or calls[id]["status"] not in allowed_statuses
            for id in fragment.sourceInvocationIds
        ):
            raise ValueError("片段引用了不存在或不符合状态要求的执行证据")
    known = {fragment.name for fragment in [*plan.skillx, *workflows]}
    for workflow in workflows:
        if check_dependencies and not set(workflow.dependsOn).issubset(known):
            raise ValueError("工作流依赖必须是本次声明的 SkillX 或工作流")
    if check_dependencies:
        order_workflows(workflows)
    if context["kind"] == "repair" and len(workflows) != 1:
        raise ValueError("单次修复必须只修订一个工作流")


def independent_workflows(workflows, skill_names):
    """Only a broken dependency subtree is blocked; atomic builders stay independent."""
    pending = {item.name: item for item in workflows}
    available = set(skill_names)
    ordered = []
    while pending:
        ready = [item for item in pending.values() if set(item.dependsOn) <= available]
        if not ready:
            break
        for item in ready:
            ordered.append(item)
            available.add(item.name)
            del pending[item.name]
    failures = {"workflow:" + name: "依赖未声明或存在循环：" +
        "、".join(sorted(set(item.dependsOn) - available)) for name, item in pending.items()}
    return ordered, failures


def order_workflows(workflows):
    pending = {item.name: item for item in workflows}
    result = []
    while pending:
        ready = [item for item in pending.values() if not set(item.dependsOn) & pending.keys()]
        if not ready:
            raise ValueError("工作流依赖存在循环")
        for item in ready:
            result.append(item)
            del pending[item.name]
    return result


def check_evidence_ids(ids: list[str], fragment: ReusableFragment) -> None:
    if not ids or not set(ids).issubset(fragment.sourceInvocationIds):
        raise ValueError("专业 Agent 的证据必须在已分配片段范围内")


def compact_builder_context(data: dict) -> dict:
    """Transmit dependency interfaces once, never their implementations or test programs."""
    from app.application.learning_context import compact_catalog_context
    from app.application.task_context import observation
    result = compact_catalog_context(data)
    for key in ("suppliedSkillXContracts", "suppliedWorkflowContracts"):
        for item in result.get(key, []):
            item.pop("examples", None)
    # Local-command evidence establishes provenance; full command transcripts are
    # retained by the host for validation, not repeated on every build/revision.
    local = any(x.get("executionProfile") == "local-command" for x in result.get("suppliedSkillXContracts", []))
    local = local or any(x.get("requiresLocalValidation") for x in result.get("suppliedWorkflowContracts", []))
    local = local or result.get("executionProfile") == "local-command"
    previous = result.get("previousDraft") or {}
    if isinstance(previous, dict) and isinstance(previous.get("workflow"), dict):
        local = local or workflow_uses_local_validation(previous["workflow"], result)
    if local:
        result["calls"] = [{"id": c["id"], "status": c["status"],
            "input": observation(c.get("input", {}), 400),
            "output": observation(c.get("output", {}), 700)} for c in result.get("calls", [])]
    # Existing published capabilities are independent of this run's generated
    # dependencies. Keep their callable contracts even when supplied lists exist.
    if "suppliedSkillXContracts" in result or "suppliedWorkflowContracts" in result:
        capabilities = result.get("catalog", {}).get("capabilities", [])
        if capabilities:
            result["catalog"] = {"capabilities": capabilities}
        else:
            result.pop("catalog", None)
        result.pop("workflowExample", None)
    return result


def workflow_uses_local_validation(definition, data):
    skills = {item["name"]: item for item in data.get("suppliedSkillXContracts", [])}
    workflows = {item["name"]: item for item in data.get("suppliedWorkflowContracts", [])}
    catalog = {item["id"]: item for item in data.get("catalog", {}).get("capabilities", [])}
    for node in (definition or {}).get("spec", {}).get("nodes", []):
        config = node.get("config", {})
        contract = skills.get(config.get("generatedSkill")) or workflows.get(config.get("generatedWorkflow"))
        if contract is None:
            published = catalog.get(config.get("capabilityVersionId") or config.get("workflowVersionId"), {})
            contract = published.get("interfaces", {}).get(config.get("interface", "execute"), {})
        if contract.get("executionProfile") == "local-command" or contract.get("requiresLocalValidation"):
            return True
    return False


def bind_workflow_schema(schema, data):
    """Expose compiler and supplied-interface constraints before generation."""
    definitions = schema.get("$defs", {})
    if "reusedDependencies" in schema.get("properties", {}):
        from app.application.dependency_reuse_contract import dependency_reuse_contract
        schema.setdefault("allOf", []).append(dependency_reuse_contract(data))
    if "WorkflowDefinition" in definitions:
        from app.domain.capability_metadata import PublicationWorkflowMetadata
        metadata = PublicationWorkflowMetadata.model_json_schema()
        definitions.update(metadata.pop("$defs", {}))
        definitions["WorkflowDefinition"]["properties"]["metadata"] = metadata
    if "WorkflowSpec" not in definitions:
        return schema
    nodes = definitions["WorkflowSpec"]["properties"]["nodes"]
    nodes["allOf"] = [{"contains": {"properties": {"type": {"const": kind}}, "required": ["type"]},
        "minContains": 1, "maxContains": 1} for kind in ("input", "output")]
    constraints = definitions["WorkflowNode"].setdefault("allOf", [])
    from app.application.extraction_graph_contract import extraction_node_contract
    constraints.append(extraction_node_contract(data))
    constraints.append({"if": {"required": ["sideEffect", "idempotent"],
        "properties": {"sideEffect": {"const": True}, "idempotent": {"const": False}}},
        "then": {"properties": {"retry": {"properties": {"maxAttempts": {"const": 1}}}}}})
    for group, reference in (("suppliedSkillXContracts", "generatedSkill"), ("suppliedWorkflowContracts", "generatedWorkflow")):
        for contract in data.get(group, []):
            required = contract.get("inputSchema", {}).get("required", [])
            if required:
                constraints.append({"if": {"required": ["config"], "properties": {"config": {
                    "required": [reference], "properties": {reference: {"const": contract["name"]}}}}},
                    "then": {"required": ["input"], "properties": {"input": {"required": required}}}})
    local_refs = []
    for item in data.get("catalog", {}).get("capabilities", []):
        for interface, contract in item.get("interfaces", {}).items():
            if contract.get("executionProfile") == "local-command" or contract.get("requiresLocalValidation"):
                for key in ("capabilityVersionId", "workflowVersionId"):
                    local_refs.append({"required": ["config"], "properties": {"config": {
                        "required": [key], "properties": {key: {"const": item["id"]},
                        "interface": {"const": interface}}}}})
    for key, group in (("generatedSkill", "suppliedSkillXContracts"), ("generatedWorkflow", "suppliedWorkflowContracts")):
        for item in data.get(group, []):
            if item.get("executionProfile") == "local-command" or item.get("requiresLocalValidation"):
                local_refs.append({"required": ["config"], "properties": {"config": {
                    "required": [key], "properties": {key: {"const": item["name"]}}}}})
    fixture = {"required": ["setup", "verify"], "properties": {
        "setup": {"type": "string", "pattern": r"\S"}, "verify": {"type": "string", "pattern": r"\S"}}}
    if local_refs and "workflowExamples" in schema.get("properties", {}):
        schema.setdefault("allOf", []).append({"if": {"required": ["workflow"], "properties": {
            "workflow": {"type": "object", "properties": {"spec": {"properties": {
                "nodes": {"contains": {"anyOf": local_refs}}}}}}}},
            "then": {"required": ["workflowExamples"], "properties": {
                "workflowExamples": {"type": "array", "minItems": 2, "items": fixture}}}})
        previous = data.get("previousDraft", {})
        if workflow_uses_local_validation(previous.get("workflow"), data):
            schema.setdefault("allOf", []).append({"properties": {"workflowExamples": {
                "type": "array", "minItems": 2, "items": fixture}}})
            if any(not example.get("setup", "").strip() or not example.get("verify", "").strip()
                   for example in previous.get("workflowExamples", [])):
                schema.setdefault("required", []).append("workflowExamples")
    return schema


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

    async def record_validation_usage(self, name, request_id, metrics):
        """Validation model calls belong in the same durable bill as builders."""
        async with SessionLocal() as session, session.begin():
            job = await self._owned_job(session)
            if "totalTokens" in metrics:
                usage = dict(job.result_json.get("usageByRequest", {}))
                usage[request_id] = metrics["totalTokens"]
                job.result_json = {**job.result_json, "usageByRequest": usage}
            common = {"jobId": self.job_id, "stage": "validation:" + name,
                "role": "workflow", "name": name}
            emit(session, self.task_id, "learning.agent.audit", {**common,
                "type": "model.request.dispatched", "payload": {"requestId": request_id,
                    "model": metrics.get("model"), "provider": metrics.get("provider")}})
            if "totalTokens" in metrics:
                emit(session, self.task_id, "learning.agent.audit", {**common,
                    "type": "agent.step.completed", "payload": {"requestId": request_id,
                        "inputTokens": metrics.get("inputTokens", 0),
                        "outputTokens": metrics.get("outputTokens", 0),
                        "usageComplete": metrics.get("usageComplete", False)}})

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

        from app.application.learning_validation_budget import validation_budget
        with validation_budget(self.job_id, lambda request_id, metrics:
                               self.record_validation_usage(name, request_id, metrics)):
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

    async def call(self, role: str, schema_model, data: dict, *, name: str = "", validate=None,
                   phase: str = ""):
        context_chars_before = len(json.dumps(data, ensure_ascii=False))
        data = compact_builder_context(data)
        context_chars_after = len(json.dumps(data, ensure_ascii=False))
        key = self.stage_key(role, name) + (":" + phase if phase else "")
        instructions = (PROMPT_ROOT / f"{role}{'_' + phase if phase else ''}.md").read_text(encoding="utf-8")
        schema = schema_model.model_json_schema()
        if role == "workflow":
            schema = bind_workflow_schema(schema, data)
        if role == "skillx" and not phase:
            model_kinds = [kind for kind in data["availableSkillKinds"] if kind != "direct"]
            if model_kinds:
                schema["$defs"]["ModelSkillXProposal"]["properties"]["kind"]["enum"] = model_kinds
            else:
                schema["$defs"]["ModelSkillXProposal"]["not"] = {}
        input_digest = digest_json({"prompt": instructions, "schema": schema, "input": data})
        cached = await self._stage(key)
        if cached.get("status") == "completed" and cached.get("inputDigest") == input_digest:
            result = schema_model.model_validate(cached["output"])
            if validate:
                validate(result)
            return result
        inputs = dict(data)
        if role == "skillx":
            import inspect
            from app.runtime.skillx import SkillXRequest, SkillXContext
            inputs["runtimeContract"] = {
                "requestType": inspect.getsource(SkillXRequest),
                "contextType": inspect.getsource(SkillXContext),
                "entrypoint": "def execute(request, context):\n    inputs = request.input\n    # Return the output-schema dict.\n",
                "note": "Host 传入上述类型实例，不是 dict。不要自行构造或替换 request/context 类型。",
            }
        if cached.get("error"):
            inputs["validationFeedback"] = {
                "error": cached["error"],
                "previousDraft": cached.get("output"),
            }
            previous_skill = (cached.get("output") or {}).get("skillx") or {}
            if not isinstance(previous_skill, dict):
                previous_skill = {}  # Extractor plans contain a list of fragments.
            implementation = (previous_skill if previous_skill.get("source") else previous_skill.get("outputSchema", {}))
            if (cached["error"].startswith("Agent 输出不符合 outputSchema")
                and implementation.get("source") and implementation.get("examples")):
                # Structural repair needs the rejected draft and exact schema,
                # not another copy of all original tool output.
                inputs.pop("calls", None)
        if cached.get("error") and cached.get("output"):
            inputs.pop("catalog", None)
            inputs.pop("workflowExample", None)
        run_id = f"learning-{self.job_id}-{role}-{uuid4()}"
        attempt = cached.get("attempt", 0) + 1
        await self._record(
            key,
            {
                "role": role,
                "roleLabel": ROLE_LABELS[role],
                "name": name,
                "status": "running",
                "error": None,
                "inputDigest": input_digest,
                "contextCharsBefore": context_chars_before,
                "contextCharsAfter": context_chars_after,
                "runId": run_id,
                "attempt": attempt,
                "startedAt": rfc3339(utcnow()),
                "finishedAt": None,
                **({"buildInputs": inputs} if role in {"workflow", "skillx"} else {}),
            },
            "learning.agent.started",
        )

        async def sink(runtime_event, audit_event):
            nonlocal raw_output
            if runtime_event.get("type") == "agent.completed":
                raw_output = runtime_event.get("payload", {}).get("output")
            async with SessionLocal() as session, session.begin():
                job = await self._owned_job(session)
                if job.lease_expires_at.replace(tzinfo=UTC) < utcnow() + timedelta(minutes=5):
                    job.lease_expires_at = utcnow() + timedelta(minutes=10)
                payload = audit_event.get("payload", {})
                if audit_event.get("type") == "agent.step.completed" and payload.get("requestId"):
                    usage = dict(job.result_json.get("usageByRequest", {}))
                    counts = [payload.get("inputTokens"), payload.get("outputTokens")]
                    complete = payload.get("usageComplete", True) and all(
                        isinstance(count, int) and not isinstance(count, bool) and count >= 0 for count in counts)
                    if complete:
                        # Provider step events can be replayed; replace by request ID,
                        # never accumulate duplicates or turn unknown usage into zero.
                        usage[payload["requestId"]] = sum(counts)
                    job.result_json = {**job.result_json, "usageByRequest": usage,
                        **({"validationUsageIncomplete": True} if not complete else {})}
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

        async def cancelled():
            async with SessionLocal() as session:
                try:
                    await self._owned_job(session)
                    return False
                except LearningLeaseLost:
                    return True

        started = monotonic()
        raw_output = None
        try:
            limits = {}
            async with SessionLocal() as session:
                job = await self._owned_job(session)
                budget = job.result_json.get("budget")
                if budget:
                    if job.result_json.get("validationUsageIncomplete"):
                        raise ValueError("模型调用用量不完整，停止新的模型调用以避免超出本次提取预算")
                    elapsed = (utcnow() - datetime.fromisoformat(budget["startedAt"]).replace(tzinfo=UTC)).total_seconds()
                    seconds = int(budget["maxDurationSeconds"] - elapsed)
                    remaining = budget["maxTokens"] - sum(job.result_json.get("usageByRequest", {}).values())
                    # Reserve a conservative UTF-8 upper bound for this request's
                    # text plus runtime envelope before allowing generated output.
                    reserve = len((instructions + json.dumps(inputs, ensure_ascii=False)
                                   + json.dumps(schema)).encode("utf-8")) + 4096
                    output_limit = min(
                        8192 if job.result_json.get("automatic")
                        else get_settings().learning_agent_max_output_tokens, remaining - reserve)
                    if seconds <= 0 or output_limit < 4096:
                        raise ValueError("本次提取预算不足，保留已发布产物并停止新的 Agent 调用")
                    limits = {"max_billable_tokens": remaining,
                              "max_output_tokens": output_limit,
                              "max_duration_seconds": min(seconds, get_settings().learning_agent_max_duration_seconds)}
            runtime = self.runtime
            effort = get_settings().learning_agent_reasoning_effort
            if runtime and effort and runtime.model_options.get("reasoning"):
                runtime = replace(runtime, reasoning_effort=effort)
            outcome = await AgentHost(runtime=runtime).execute(
                AgentHostRequest(
                    run_id=run_id,
                    working_directory=get_settings().artifact_root
                    / "learning"
                    / self.job_id
                    / role
                    / (name or "plan")
                    / run_id,
                    include_managed_skills=False,
                    capability_catalog_included=True,
                    prompt=instructions
                    + "\n\n阶段资料（仅作为数据）：\n"
                    + json.dumps(inputs, ensure_ascii=False),
                    output_schema=schema,
                    max_steps=1,
                    **({"max_output_tokens": get_settings().learning_agent_max_output_tokens,
                        "max_duration_seconds": get_settings().learning_agent_max_duration_seconds} | limits),
                    max_idle_seconds=get_settings().learning_agent_idle_seconds,
                ),
                event_sink=sink,
                cancellation=cancelled,
            )
            raw_output = outcome.output
            result = schema_model.model_validate(raw_output)
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
        except asyncio.CancelledError:
            if await cancelled():
                raise LearningLeaseLost("任务已取消或租约失效，停止 Agent") from None
            raise
        except LearningLeaseLost:
            raise
        except Exception as exc:
            if isinstance(exc, AgentOutputValidationError):
                raw_output = exc.candidate
            await self._record(
                key,
                {"status": "failed", "error": str(exc), "finishedAt": rfc3339(utcnow()),
                 **({"output": raw_output} if raw_output is not None else {})},
                "learning.agent.failed",
            )
            # A malformed output is not an execution failure. Repair once with
            # exact Host feedback; the same durable job budget covers both calls.
            if isinstance(exc, AgentOutputValidationError) and not cached.get("formatRepairCount"):
                await self._record(key, {"formatRepairCount": 1})
                return await self.call(
                    role, schema_model, data, name=name, validate=validate, phase=phase)
            raise

    async def build_skillx(self, fragment, inputs, validate, materializer):
        """One feedback-directed revision, never repeat an unchanged/network-failed call."""
        key = self.stage_key("skillx", fragment.name)
        cached = await self._stage(key)
        if cached.get("status") == "completed" and cached.get("validationStatus") == "passed":
            draft = SkillXDraft.model_validate(cached["output"])
            validate(draft)
            self.materialized_roots[fragment.name] = await self.validate_skillx(
                fragment.name, lambda: materializer(draft.skillx, fragment.sourceInvocationIds))
            return draft
        if (cached.get("status") == "needs_revision" and cached.get("validationStatus") == "failed"
            and cached.get("output")):
            try:
                draft = SkillXDraft.model_validate(cached["output"])
            except ValueError:
                draft = None
            if draft and draft.skillx:
                try:
                    validate(draft)
                    self.materialized_roots[fragment.name] = await self.validate_skillx(
                        fragment.name, lambda: materializer(draft.skillx, fragment.sourceInvocationIds))
                    await self._record(key, {"status": "completed", "error": None})
                    return draft
                except LearningLeaseLost:
                    raise
                except (ValueError, OutputSchemaError):
                    if cached.get("revisionCount", 0) >= 1:
                        raise  # No new feedback budget merely because a worker restarted.
        while True:
            try:
                stage = await self._stage(key)
                partial = (stage.get("output") or {}).get("skillx") or {}
                import ast
                try:
                    has_entrypoint = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == "execute" for node in ast.parse(partial.get("source", "")).body)
                except SyntaxError:
                    has_entrypoint = False
                if (partial.get("kind", "direct") == "direct" and not has_entrypoint
                    and partial.get("inputSchema") and partial.get("outputSchema")):
                    draft = await self.complete_implementation(fragment, inputs, partial, validate)
                elif stage.get("status") == "needs_revision" and has_entrypoint:
                    draft = await self.repair_skillx(fragment, stage, validate)
                else:
                    draft = await self.call("skillx", SkillXDraft, inputs,
                        name=fragment.name, validate=validate)
                if draft.decision == "ready":
                    self.materialized_roots[fragment.name] = await self.validate_skillx(
                        fragment.name, lambda: materializer(draft.skillx, fragment.sourceInvocationIds))
                return draft
            except LearningLeaseLost:
                raise
            except (ValueError, OutputSchemaError, AgentHostError) as exc:
                if isinstance(exc, AgentHostError) and exc.code != "CONTRACT_VIOLATION":
                    raise
                stage = await self._stage(key)
                # Only a concrete model draft + deterministic validation feedback
                # can trigger correction. Persist the bound across worker resumes.
                if not stage.get("output") or stage.get("revisionCount", 0) >= 1:
                    raise
                await self._record(key, {"revisionCount": 1, "status": "needs_revision"})

    async def repair_skillx(self, fragment, stage, validate):
        """Ask for a validated delta; keep unchanged contract/code/tests verbatim."""
        original = stage["output"]
        original_skill = original["skillx"]
        def merge(patch):
            changes = patch.model_dump(exclude_none=True)
            if not changes:
                raise ValueError("修订没有针对失败反馈提供任何变更")
            result = SkillXDraft.model_validate({**original,
                "skillx": {**original_skill, **changes}})
            validate(result)
            return result
        patch = await self.call("skillx", SkillXRepair,
            {"fragment": fragment.model_dump(), "previousDraft": original_skill,
             "calls": stage.get("buildInputs", {}).get("calls", []),
             "validationFeedback": stage.get("error"),
             "fixtureContract": "setup 写 .skillx-fixture.json，input 完整字符串 ${fixture.key} 引用动态值。"},
            name=fragment.name, phase="repair", validate=merge)
        draft = merge(patch)
        await self._record(self.stage_key("skillx", fragment.name), {
            "status": "completed", "error": None, "output": draft.model_dump()}, "learning.agent.completed")
        return draft

    async def complete_implementation(self, fragment, inputs, partial, validate):
        """Keep the Agent's contract; ask only for the missing executable artifact."""
        contract = {key: value for key, value in partial.items() if key not in {"source", "examples"}}
        data = {**inputs, "contract": contract}
        stage = await self._stage(self.stage_key("skillx", fragment.name))
        data["validationFeedback"] = {"error": stage.get("error"), "previousDraft": stage.get("output")}
        if contract.get("executionProfile") == "local-command":
            from app.application.task_context import observation
            data["calls"] = [{"id": call["id"], "status": call["status"],
                "input": observation(call.get("input", {}), 400),
                "output": observation(call.get("output", {}), 700)} for call in inputs["calls"]]

        def assemble(implementation):
            draft = SkillXDraft.model_validate({"decision": "ready", "reason": "Agent 完成独立实现阶段",
                "skillx": {**contract, **implementation.model_dump()}})
            validate(draft)
            return draft

        implementation = await self.call("skillx", SkillXImplementation, data,
            name=fragment.name, phase="implementation", validate=assemble)
        draft = assemble(implementation)
        await self._record(self.stage_key("skillx", fragment.name), {
            "status": "completed", "error": None, "output": draft.model_dump(),
        }, "learning.agent.completed")
        return draft

    async def revise_workflow(self, flow, error):
        key = self.stage_key("workflow", flow.name)
        stage = await self._stage(key)
        await self.invalidate("workflow", flow.name, str(error))
        if not stage.get("buildInputs") or stage.get("revisionCount", 0) >= 1:
            return None
        await self._record(key, {"revisionCount": 1})
        from copy import deepcopy
        from app.application.learning_dependency_repair import repair_targets, check_requests, repair_dependencies
        targets = await repair_targets(getattr(error, "observations", []))
        build_inputs = deepcopy(stage["buildInputs"])
        from app.application.learning_counterexamples import validate_counterexamples
        counterexamples = deepcopy(stage.get("counterexamples", []))
        failure_case = getattr(error, "counterexample", None)
        if failure_case and all(item["id"] != failure_case["id"] for item in counterexamples):
            counterexamples.append(failure_case)
        if counterexamples:
            await self._record(key, {"counterexamples": counterexamples})
        # A prior binding repair says nothing about the current failure type.
        # Topology, retry policy and fixture errors cannot be fixed by inputs alone.
        def merge(patch):
            changes = patch.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
            scope_change = changes.pop("scopeChange", None)
            requests = changes.pop("dependencyRepairs", None)
            if requests:
                check_requests(patch.dependencyRepairs, targets)
            if not changes and not requests:
                raise ValueError("工作流修订不能为空")
            result = WorkflowDraft.model_validate({"decision": "ready", "reason": "Agent 定向修订",
                "workflow": flow.definition, "workflowExamples": [e.model_dump() for e in flow.examples],
                "reusedDependencies": stage.get("output", {}).get("reusedDependencies", {}),
                **changes})
            from app.application.input_form_coverage import check_declared_scope_change
            check_declared_scope_change(flow.definition["spec"]["inputSchema"], result.workflow["spec"]["inputSchema"],
                                        scope_change, flow.definition["metadata"], result.workflow["metadata"])
            if scope_change:
                result.reason += "：" + scope_change
            from app.application.dependency_reuse_contract import validate_dependency_reuse
            validate_dependency_reuse(result, build_inputs)
            from app.application.extraction_graph_contract import validate_extraction_nodes
            validate_extraction_nodes(result.workflow, build_inputs)
            validate_counterexamples(result.workflowExamples, counterexamples)
            return result
        patch = await self.call("workflow", WorkflowRepair,
            {"fragment": build_inputs["fragment"],
             "suppliedSkillXContracts": build_inputs.get("suppliedSkillXContracts", []),
             "suppliedWorkflowContracts": build_inputs.get("suppliedWorkflowContracts", []),
             "catalog": build_inputs.get("catalog", {}),
             "skippedDependencies": build_inputs.get("skippedDependencies", []),
             "calls": build_inputs.get("calls", []),
             "previousDraft": {"workflow": flow.definition, "workflowExamples": [e.model_dump() for e in flow.examples]},
             "dependencyRepairTargets": targets,
             "requiredCounterexamples": counterexamples,
             "validationFeedback": {"error": str(error)}}, name=flow.name, phase="repair", validate=merge)
        if patch.dependencyRepairs:
            replacements = await repair_dependencies(self, patch.dependencyRepairs, targets)
            # Resume the same workflow revision with the Agent-issued new locks;
            # no automatic graph edits and no additional retry allowance.
            build_inputs.setdefault("catalog", {}).setdefault("capabilities", []).extend(replacements)
            def rebind_validate(value):
                if value.dependencyRepairs:
                    raise ValueError("同一次工作流修订不能链式追加依赖修订")
                revised = merge(value)
                configs = [node.get("config", {}) for node in revised.workflow["spec"]["nodes"]]
                for replacement in replacements:
                    if any(config.get("capabilityVersionId") == replacement["previousVersionId"] for config in configs):
                        raise ValueError("工作流仍引用已证实缺陷的旧依赖版本")
                    if not any(config.get("capabilityVersionId") == replacement["id"] or
                        config.get("generatedSkill") == replacement["name"] for config in configs):
                        raise ValueError("依赖修订后必须由 Agent 显式绑定新版本，不能删除原业务步骤")
                return revised
            patch = await self.call("workflow", WorkflowRepair,
                {**build_inputs, "previousDraft": {"workflow": flow.definition,
                    "workflowExamples": [e.model_dump() for e in flow.examples]},
                 "repairedDependencies": replacements,
                 "requiredCounterexamples": counterexamples,
                 "validationFeedback": {"error": str(error), "actualObservations": targets,
                    "action": "依赖已由其 Agent 修订并验证发布。更新图中锁定版本/digest与复用声明，保留失败场景后重新验证；不要删除断言或再次请求依赖修订。"}},
                name=flow.name, phase="dependency_bindings", validate=rebind_validate)
        draft = merge(patch)
        await self._record(key, {"output": draft.model_dump(), "status": "completed", "error": None},
            "learning.agent.completed")
        return flow.model_copy(update={"definition": draft.workflow, "examples": draft.workflowExamples})

    async def repair_workflow_draft(self, fragment, inputs, error, validate):
        """Route rejected structures through the same single Agent revision budget."""
        key = self.stage_key("workflow", fragment.name)
        stage = await self._stage(key)
        original = stage.get("output")
        if not isinstance(original, dict) or not original or stage.get("revisionCount", 0) >= 1:
            raise ValueError(error)
        await self._record(key, {"revisionCount": 1, "status": "needs_revision"})

        def merge(patch):
            changes = patch.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)
            if not changes:
                raise ValueError("工作流修订不能为空")
            scope_change = changes.pop("scopeChange", None)
            result = WorkflowDraft.model_validate({**original, **changes})
            from app.application.input_form_coverage import check_declared_scope_change
            before = original.get("workflow") or {}
            check_declared_scope_change(before.get("spec", {}).get("inputSchema", {}), result.workflow["spec"]["inputSchema"],
                                        scope_change, before.get("metadata"), result.workflow["metadata"])
            if scope_change:
                result.reason += "：" + scope_change
            validate(result)
            return result

        patch = await self.call("workflow", WorkflowRepair,
            {"fragment": inputs["fragment"], "previousDraft": original,
             "skippedDependencies": inputs.get("skippedDependencies", []),
             "suppliedSkillXContracts": inputs.get("suppliedSkillXContracts", []),
             "suppliedWorkflowContracts": inputs.get("suppliedWorkflowContracts", []),
             "calls": inputs.get("calls", []), "catalog": inputs.get("catalog", {}),
             "validationFeedback": {"error": error}},
            name=fragment.name, phase="repair", validate=merge)
        result = merge(patch)
        await self._record(key, {"status": "completed", "error": None,
            "output": result.model_dump(), "buildInputs": inputs}, "learning.agent.completed")
        return result

    async def build_workflow(self, fragment, inputs, validate):
        key = self.stage_key("workflow", fragment.name)
        stage = await self._stage(key)
        draft = stage.get("output") or {}
        if draft.get("workflow"):
            try:
                WorkflowDefinition.model_validate(draft["workflow"])
            except ValueError as exc:
                return await self.repair_workflow_draft(fragment, inputs, str(exc), validate)
        if (stage.get("status") == "completed" and draft.get("workflow")
            and draft.get("workflowExamples") and not missing_bindings(draft["workflow"], inputs)
            and (await self._stage(key + ":bindings")).get("status") != "needs_revision"):
            result = WorkflowDraft.model_validate(draft)
            validate(result)
            return result
        bindings_stage = await self._stage(key + ":bindings")
        if draft.get("workflow") and (missing_bindings(draft["workflow"], inputs)
                                      or bindings_stage.get("status") == "needs_revision"):
            from copy import deepcopy
            original = deepcopy(draft)
            data = {"fragment": inputs["fragment"], "workflow": draft["workflow"],
                "workflowExamples": draft.get("workflowExamples", []),
                "validationError": stage.get("error"),
                "missingMappings": missing_bindings(draft["workflow"], inputs),
                "suppliedSkillXContracts": inputs.get("suppliedSkillXContracts", []),
                "suppliedWorkflowContracts": inputs.get("suppliedWorkflowContracts", [])}

            def wire(bindings):
                candidate = deepcopy(original)
                nodes = {node["id"]: node for node in candidate["workflow"]["spec"]["nodes"]}
                for id, mapping in bindings.nodeInputs.items():
                    if id not in nodes:
                        raise ValueError(f"映射引用不存在的节点 {id}")
                    nodes[id]["input"] = mapping
                missing = missing_bindings(candidate["workflow"], inputs)
                if missing:
                    raise ValueError(f"节点 input 映射仍缺字段：{missing}")
                return candidate

            bindings = await self.call("workflow", WorkflowBindings, data, name=fragment.name,
                phase="bindings", validate=wire)
            draft = wire(bindings)
            await self._record(key, {"output": draft})
            if draft.get("workflowExamples"):
                result = WorkflowDraft.model_validate(draft)
                validate(result)
                await self._record(key, {"status": "completed", "error": None,
                    "output": result.model_dump(), "buildInputs": inputs}, "learning.agent.completed")
                return result
        if not (draft.get("workflow") and not draft.get("workflowExamples")):
            try:
                return await self.call("workflow", WorkflowDraft, inputs,
                    name=fragment.name, validate=validate)
            except (ValueError, OutputSchemaError, AgentHostError) as exc:
                if isinstance(exc, AgentHostError) and exc.code != "CONTRACT_VIOLATION":
                    raise
                stage = await self._stage(key)
                draft = stage.get("output") or {}
                if draft.get("workflow"):
                    try:
                        WorkflowDefinition.model_validate(draft["workflow"])
                    except ValueError:
                        return await self.repair_workflow_draft(fragment, inputs, str(exc), validate)
                    if missing_bindings(draft["workflow"], inputs):
                        return await self.build_workflow(fragment, inputs, validate)
                if not (draft.get("workflow") and not draft.get("workflowExamples")):
                    return await self.repair_workflow_draft(fragment, inputs, str(exc), validate)
        from app.application.task_context import observation
        data = {"fragment": inputs["fragment"], "workflow": draft["workflow"],
            "suppliedSkillXContracts": inputs.get("suppliedSkillXContracts", []),
            "suppliedWorkflowContracts": inputs.get("suppliedWorkflowContracts", []),
            "calls": [{"id": call["id"], "status": call["status"],
                "input": observation(call.get("input", {}), 400),
                "output": observation(call.get("output", {}), 700)} for call in inputs["calls"]]}
        if not (any(item.get("executionProfile") == "local-command" for item in data["suppliedSkillXContracts"])
                or any(item.get("requiresLocalValidation") for item in data["suppliedWorkflowContracts"])):
            data["calls"] = inputs["calls"]
        examples = await self.call("workflow", WorkflowExamples, data, name=fragment.name, phase="examples")
        result = WorkflowDraft.model_validate({**draft, "workflowExamples": examples.model_dump()["workflowExamples"]})
        validate(result)
        await self._record(key, {"status": "completed", "output": result.model_dump(), "error": None,
            "buildInputs": inputs}, "learning.agent.completed")
        return result

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

    async def generate(self, context: dict, *, materializer, publish=None) -> LearningProposal:
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
                "requirements",
                "learningNomination",
            )
            if key in context
        }
        from app.application.task_context import observation
        # Extraction selects boundaries; specialists receive the exact assigned evidence.
        # Never repeat source code and tool outputs inside decisions and conversation state.
        extraction_context["calls"] = [
            {"id": call["id"], "kind": call.get("kind"), "status": call["status"],
             "input": observation(call.get("input", {}), 500),
             "output": observation(call.get("output", {}), 700),
             "error": observation(call.get("error"), 300)}
            for call in context["calls"]
        ]
        extraction_context["decisions"] = [
            {key: observation(value, 300) for key, value in decision.items() if key in {"summary", "reason", "status"}}
            for decision in context.get("decisions", [])
        ]
        # Boundary selection needs names and interface descriptions, not every
        # nested workflow schema. Builders still receive the complete catalog.
        extraction_context["catalog"] = {
            "capabilities": [
                {"id": item.get("id"), "name": item.get("name"),
                 "description": observation(item.get("description", ""), 300),
                 "interfaces": list(item.get("interfaces", {}))}
                for item in context.get("catalog", {}).get("capabilities", [])
            ],
            "tools": [{"capabilityRef": item["capabilityRef"], "effect": item.get("effect")}
                for item in context.get("catalog", {}).get("tools", [])],
        }
        for key in ("sessionMessages", "humanAnswers", "outcome"):
            if key in extraction_context:
                extraction_context[key] = observation(extraction_context[key], 1500)
        saved_plan = await self._stage("extractor")
        plan_model = ComposedExtractionPlan if context.get("requirements", {}).get("requireWorkflowComposition") else ExtractionPlan
        if saved_plan.get("status") == "completed":
            # A learning job owns an immutable source snapshot. Resuming it must
            # not re-plan merely because its earlier stages changed the catalog.
            plan = plan_model.model_validate(saved_plan["output"])
            validate_extraction(plan, context, check_dependencies=False)
        else:
            try:
                plan = await self.call("extractor", plan_model, extraction_context,
                    validate=lambda value: validate_extraction(value, context, check_dependencies=False))
            except ValueError:
                stage = await self._stage("extractor")
                if not stage.get("output") or stage.get("revisionCount", 0) >= 1:
                    raise
                await self._record("extractor", {"revisionCount": 1})
                plan = await self.call("extractor", plan_model, extraction_context,
                    validate=lambda value: validate_extraction(value, context, check_dependencies=False))
        if plan.decision == "skip":
            return LearningProposal(decision="skip", reason=plan.reason)
        workflows = [*plan.workflows, *([plan.workflow] if plan.workflow else [])]
        known = {item.name for item in [*plan.skillx, *workflows]}
        unresolved = [item for item in workflows if not set(item.dependsOn) <= known]
        if unresolved:
            # Resolve identifiers against a finite symbol table, never fuzzy-match
            # or regenerate the plan. A failed binding cannot block atomic builders.
            fields = {}
            for item in unresolved:
                names = tuple(sorted(known - {item.name}))
                fields[item.name] = ((list[Literal[names]], ...) if names else
                                     (list[str], Field(max_length=0)))
            Bindings = create_model("ExtractionDependencyBindings", __config__=ConfigDict(extra="forbid"), **fields)
            try:
                if (await self._stage("extractor:dependencies")).get("status") == "failed":
                    raise ValueError("依赖绑定已失败，不因恢复任务重复调用")
                def validate_bindings(value):
                    patches = value.model_dump()
                    order_workflows([item.model_copy(update={"dependsOn": patches.get(item.name, item.dependsOn)})
                                     for item in workflows])
                bindings = await self.call("extractor", Bindings,
                    {"fragments": [{"name": item.name, "description": item.description}
                                   for item in [*plan.skillx, *workflows]],
                     "unresolved": [item.model_dump() for item in unresolved]},
                    phase="dependencies", validate=validate_bindings)
                for item in unresolved:
                    item.dependsOn = bindings.model_dump()[item.name]
                await self._record("extractor", {"output": plan.model_dump()})
            except LearningLeaseLost:
                raise
            except Exception:
                pass  # Per-workflow dependency failures are recorded below.
        self.workflow_name = plan.workflow.name if plan.workflow else ""
        proposal = LearningProposal(decision="candidate", reason=plan.reason)
        async def publish_one(group, artifact):
            if publish is None:
                return artifact
            _, result = await publish(LearningProposal(decision="candidate", reason=plan.reason,
                **{group: [artifact]}))
            proposal.failures.update(result["failures"])
            key = ("workflow" if group == "workflows" else group) + ":" + artifact.name
            if artifact.name not in result["artifacts"][group]:
                return None
            proposal.failures.pop(key, None)
            if group == "workflows":
                # Publication may ask the Agent to repair the child. The parent
                # must see that validated contract, not its rejected first draft.
                saved = await self._stage(self.stage_key("workflow", artifact.name))
                draft = WorkflowDraft.model_validate(saved["output"])
                return artifact.model_copy(update={"definition": draft.workflow,
                    "examples": draft.workflowExamples})
            return artifact
        async with SessionLocal() as session:
            published = (await self._owned_job(session)).result_json.get("artifacts", {})
        skipped = []
        for fragment in plan.skills:
            try:
                saved = await self._stage(self.stage_key("skill", fragment.name))
                if fragment.name in published.get("skills", {}) and saved.get("status") == "completed":
                    proposal.skills.append(SkillDraft.model_validate(saved["output"]).skill)
                    continue
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
                    artifact = await publish_one("skills", draft.skill)
                    if artifact is not None:
                        proposal.skills.append(artifact)
                else:
                    skipped.append(draft.reason)
            except LearningLeaseLost:
                raise
            except Exception as exc:
                proposal.failures["skill:" + fragment.name] = str(exc)
        for fragment in plan.skillx:
            try:
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
                    example_ids = [example.sourceInvocationId for example in value.skillx.examples
                                   if example.sourceInvocationId]
                    if example_ids or value.skillx.executionProfile != "local-command":
                        check_evidence_ids(example_ids, fragment)

                draft = await self.build_skillx(fragment, inputs, validate_skillx, materializer)
                if draft.decision == "ready":
                    artifact = await publish_one("skillx", draft.skillx)
                    if artifact is not None:
                        proposal.skillx.append(artifact)
                else:
                    skipped.append(draft.reason)
            except LearningLeaseLost:
                raise
            except Exception as exc:
                proposal.failures["skillx:" + fragment.name] = str(exc)
        ordered, dependency_failures = independent_workflows(
            [*plan.workflows, *([plan.workflow] if plan.workflow else [])],
            {fragment.name for fragment in plan.skillx})
        proposal.failures.update(dependency_failures)
        for fragment in ordered:
            if fragment.reuseVersionId:
                from app.infrastructure.database import WorkflowVersion
                async with SessionLocal() as reuse_session:
                    version = await reuse_session.get(WorkflowVersion, fragment.reuseVersionId)
                available = {item.get("id") for item in context.get("catalog", {}).get("capabilities", [])}
                if not version or fragment.reuseVersionId not in available:
                    proposal.failures["workflow:" + fragment.name] = "复用目标必须是目录中已发布的工作流版本"
                    continue
                await self._record(self.stage_key("workflow", fragment.name), {
                    "role": "workflow", "name": fragment.name,
                    "status": "completed", "output": {"decision": "skip",
                        "reason": "提炼 Agent 已选择复用工作流版本 " + fragment.reuseVersionId},
                    "reusedVersionId": fragment.reuseVersionId,
                }, "learning.agent.completed")
                continue
            saved = await self._stage(self.stage_key("workflow", fragment.name))
            if fragment.name in published.get("workflows", {}) and saved.get("status") == "completed":
                draft = WorkflowDraft.model_validate(saved["output"])
                proposal.workflows.append(WorkflowProposal(
                    name=fragment.name, definition=draft.workflow, scope=fragment.scope,
                    sourceInvocationIds=fragment.sourceInvocationIds,
                    dependsOn=fragment.dependsOn, examples=draft.workflowExamples))
                continue
            missing = set(fragment.dependsOn) - {item.name for item in [*proposal.skillx, *proposal.workflows]}
            skipped_dependencies = []
            for name in sorted(missing):
                for role in ("skillx", "workflow"):
                    dependency_stage = await self._stage(self.stage_key(role, name))
                    output = dependency_stage.get("output") or {}
                    if dependency_stage.get("status") == "completed" and output.get("decision") == "skip":
                        original = next(item for item in [*plan.skillx, *workflows] if item.name == name)
                        skipped_dependencies.append({"name": name, "scope": original.scope, "reason": output["reason"],
                            **({"versionId": dependency_stage["reusedVersionId"]} if dependency_stage.get("reusedVersionId") else {})})
                        break
            unavailable = missing - {item["name"] for item in skipped_dependencies}
            if missing and not context.get("catalog", {}).get("capabilities"):
                unavailable = missing
            if unavailable:
                # Failed/unpublished dependencies cannot be silently replaced.
                proposal.failures["workflow:" + fragment.name] = "依赖未构建：" + "、".join(sorted(unavailable))
            else:
                inputs = {
                    **self.fragment_context(context, fragment),
                    "catalog": context["catalog"],
                    "skippedDependencies": skipped_dependencies,
                    "workflowExample": context["workflowExample"],
                    "suppliedWorkflowContracts": [
                        {"name": item.name, "inputSchema": item.definition.get("spec", {}).get("inputSchema", {}),
                         "outputSchema": item.definition.get("spec", {}).get("outputSchema", {}),
                         "examples": [example.model_dump() for example in item.examples],
                         "requiresLocalValidation": any(example.setup for example in item.examples)}
                        for item in proposal.workflows if item.name in fragment.dependsOn
                    ],
                    "suppliedSkillXContracts": [
                        {
                            "name": skill.name,
                            "description": skill.description,
                            "inputSchema": skill.inputSchema,
                            "outputSchema": skill.outputSchema,
                            "interface": "execute",
                            "kind": skill.kind,
                            "executionProfile": skill.executionProfile,
                            "examples": [example.model_dump() for example in skill.examples]
                                if skill.executionProfile == "local-command" else [],
                            "validationStatus": "passed",
                        }
                        for skill in proposal.skillx
                        if skill.name in fragment.dependsOn
                    ],
                }
                if "failure" in context:
                    inputs["failure"] = context["failure"]
                # A composition with explicitly bound children does not need
                # unrelated atomic implementations or every historical workflow.
                if fragment.dependsOn and all(item.get("versionId") for item in skipped_dependencies):
                    generated_names = {item.name for item in proposal.workflows}
                    reused_names = {item["name"] for item in skipped_dependencies}
                    if set(fragment.dependsOn) <= generated_names | reused_names:
                        pinned_versions = {item["versionId"] for item in skipped_dependencies}
                        inputs["catalog"] = {**context["catalog"], "capabilities": [
                            item for item in context["catalog"].get("capabilities", [])
                            if item.get("id") in pinned_versions]}
                if (any(item.get("executionProfile") == "local-command" for item in inputs["suppliedSkillXContracts"])
                    or any(item.get("requiresLocalValidation") for item in inputs["suppliedWorkflowContracts"])):
                    inputs["calls"] = [{"id": call["id"], "status": call["status"],
                        "input": observation(call.get("input", {}), 400),
                        "output": observation(call.get("output", {}), 700)} for call in inputs["calls"]]

                def validate_workflow(value):
                    if value.decision == "skip":
                        if value.workflow is not None:
                            raise ValueError("skip 不能同时生成工作流")
                        return
                    if not value.workflow or not value.workflowExamples:
                        raise ValueError("工作流必须提供定义和验证样例")
                    from app.application.dependency_reuse_contract import validate_dependency_reuse
                    validate_dependency_reuse(value, inputs)
                    from app.application.extraction_graph_contract import validate_extraction_nodes
                    validate_extraction_nodes(value.workflow, inputs)
                    example_ids = [example.sourceInvocationId for example in value.workflowExamples
                                   if example.sourceInvocationId]
                    local = workflow_uses_local_validation(value.workflow, inputs)
                    if local and any(not example.setup.strip() or not example.verify.strip() for example in value.workflowExamples):
                        raise ValueError("命令验证必须使用独立临时环境与后置条件：workflowExamples 每项必须提供非空 setup 和 verify")
                    if example_ids or not local:
                        check_evidence_ids(example_ids, fragment)
                    for node in value.workflow.get("spec", {}).get("nodes", []):
                        generated = node.get("config", {}).get("generatedSkill") or node.get("config", {}).get("generatedWorkflow")
                        if generated and (generated not in fragment.dependsOn or generated in missing):
                            raise ValueError("工作流引用了未分配或已跳过的生成依赖；请直接调用已发布能力")
                    missing_inputs = missing_bindings(value.workflow, inputs)
                    if missing_inputs:
                        raise ValueError(f"节点缺少 input 映射：{missing_inputs}；边只控制执行顺序，不会传递参数，请返回含完整映射的工作流图")

                try:
                    draft = await self.build_workflow(fragment, inputs, validate_workflow)
                    if draft.decision == "ready":
                        if context["kind"] == "repair":
                            proposal.workflow = draft.workflow
                            proposal.workflowExamples = draft.workflowExamples
                            proposal.baseVersionId = draft.baseVersionId
                            proposal.workflowScope = fragment.scope
                            proposal.workflowSourceInvocationIds = fragment.sourceInvocationIds
                        else:
                            artifact = await publish_one("workflows", WorkflowProposal(
                                name=fragment.name, definition=draft.workflow, scope=fragment.scope,
                                sourceInvocationIds=fragment.sourceInvocationIds,
                                dependsOn=[name for name in fragment.dependsOn if name not in draft.reusedDependencies],
                                examples=draft.workflowExamples,
                            ))
                            if artifact is not None:
                                proposal.workflows.append(artifact)
                    else:
                        skipped.append(draft.reason)
                except LearningLeaseLost:
                    raise
                except Exception as exc:
                    proposal.failures["workflow:" + fragment.name] = str(exc)
        if skipped:
            proposal.reason += "；" + "；".join(skipped)
        if not proposal.skills and not proposal.skillx and not proposal.workflow and not proposal.workflows and not proposal.failures:
            proposal.decision = "skip"
        return proposal
