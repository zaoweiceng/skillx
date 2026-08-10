from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import WorkflowCompiler, digest_json
from app.application.workflow_catalog import (
    CatalogError,
    resolve_fragment_manifest,
    resolve_workflow_manifest,
)
from app.config import get_settings
from app.domain.workflow import NodeType, WorkflowDefinition
from app.infrastructure.database import PlanSnapshot, WorkflowVersion, utcnow

_REF = re.compile(r"\$\{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>[^}]+)}")


class PlanStep(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    kind: Literal["workflow", "fragment", "transform", "llm", "agent"]
    ref: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class PlanEdge(BaseModel):
    source: str
    target: str

    model_config = ConfigDict(extra="forbid")


class ExecutionPlan(BaseModel):
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    steps: list[PlanStep] = Field(min_length=1)
    edges: list[PlanEdge] = Field(default_factory=list)
    output: dict[str, Any]
    policies: dict[str, Any] = Field(
        default_factory=lambda: {"maxDurationSeconds": 300, "maxParallelNodes": 4}
    )

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class BindingSource(BaseModel):
    field: str
    source: Literal["user_message", "history_message", "tool_result"]
    message_id: str | None = Field(default=None, alias="messageId")
    call_id: str | None = Field(default=None, alias="callId")
    path: str | None = None
    quote: str
    confidence: float = Field(ge=0, le=1)
    uncertain: bool = False
    server_verified: bool = Field(default=False, alias="serverVerified")
    normalization_type: str | None = Field(default=None, alias="normalizationType")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WorkflowSelection(BaseModel):
    action: Literal[
        "run_full", "run_fragment", "compose", "continue_agent", "ask_user"
    ]
    reason: str = ""
    refs: list[str] = Field(default_factory=list)
    bindings: dict[str, Any] = Field(default_factory=dict)
    binding_sources: list[BindingSource] = Field(
        default_factory=list, alias="bindingSources"
    )
    missing_inputs: list[str] = Field(default_factory=list, alias="missingInputs")
    question: str | None = None
    plan: dict[str, Any] | None = None
    compiled_plan: dict[str, Any] | None = Field(default=None, alias="compiledPlan")
    required_capabilities: list[str] = Field(
        default_factory=list, alias="requiredCapabilities"
    )
    session_id: str = Field(alias="sessionId")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="after")
    def validate_action_contract(self) -> WorkflowSelection:
        if self.action == "ask_user":
            if not self.question or not self.missing_inputs:
                raise ValueError("ask_user 需要 question 和 missingInputs")
            if self.plan is None and len(self.refs) > 1:
                raise ValueError("非 compose ask_user 最多保留一个已披露 ref")
        elif self.action == "continue_agent":
            if not self.reason:
                raise ValueError("continue_agent 需要 reason")
            if self.refs or self.plan is not None or self.compiled_plan is not None:
                raise ValueError("continue_agent 禁止 refs/plan")
        elif self.required_capabilities:
            raise ValueError("requiredCapabilities 仅用于 continue_agent")
        elif self.action == "run_full":
            if len(self.refs) != 1 or not self.refs[0].startswith("workflow:"):
                raise ValueError("run_full 需要恰好一个 workflowRef")
            if self.plan is not None or self.compiled_plan is not None:
                raise ValueError("run_full 禁止 plan")
        elif self.action == "run_fragment":
            if len(self.refs) != 1 or not self.refs[0].startswith(
                ("fragment:", "fragment-snapshot:")
            ):
                raise ValueError("run_fragment 需要恰好一个 fragmentRef")
            if self.plan is not None or self.compiled_plan is not None:
                raise ValueError("run_fragment 禁止 plan")
        elif self.action == "compose":
            if self.plan is None:
                raise ValueError("compose 需要 ExecutionPlan")
        return self

    @model_validator(mode="after")
    def validate_binding_source_coverage(
        self, info: ValidationInfo
    ) -> WorkflowSelection:
        if info.context and info.context.get("manual_execution"):
            return self
        source_fields = [item.field for item in self.binding_sources]
        if (
            set(source_fields) != set(self.bindings)
            or len(source_fields) != len(set(source_fields))
        ):
            raise ValueError("每个 binding 必须有且仅有一个同字段 bindingSource")
        return self


class PlanCompileError(ValueError):
    pass


def _replace_refs(value: Any, replacements: dict[str, str], prefix: str) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            root = match.group("root")
            path = match.group("path")
            if root == "input":
                mapped = replacements.get(path.split(".", 1)[0])
                if mapped is None:
                    raise PlanCompileError(f"步骤缺少输入映射: {path}")
                suffix = path.split(".", 1)[1] if "." in path else ""
                if suffix and mapped.endswith("}"):
                    return _replace_plan_refs(mapped[:-1] + "." + suffix + "}")
                return _replace_plan_refs(mapped)
            return f"${{{prefix}{root}.{path}}}"

        return _REF.sub(replace, value)
    if isinstance(value, dict):
        return {key: _replace_refs(item, replacements, prefix) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_refs(item, replacements, prefix) for item in value]
    return value


def _replace_plan_refs(value: Any) -> Any:
    if isinstance(value, str):
        return _REF.sub(
            lambda match: (
                match.group(0)
                if match.group("root") == "input"
                else f"${{{match.group('root')}.output.{match.group('path')}}}"
            ),
            value,
        )
    if isinstance(value, dict):
        return {key: _replace_plan_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_plan_refs(item) for item in value]
    return value


def _node_copy(node: Any, step_id: str, input_mapping: dict[str, Any]) -> dict[str, Any]:
    raw = node.model_dump(by_alias=True, mode="json")
    raw["id"] = f"{step_id}__{node.id}"
    raw["input"] = _replace_refs(raw.get("input", {}), input_mapping, f"{step_id}__")
    fallback = raw.get("fallback")
    if fallback:
        fallback["target"] = f"{step_id}__{fallback['target']}"
    return raw


def _expand_definition(
    definition: WorkflowDefinition,
    step: PlanStep,
    *,
    included: set[str] | None = None,
    output_mapping: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    internal = [
        node
        for node in definition.spec.nodes
        if node.type not in {NodeType.INPUT, NodeType.OUTPUT}
        and (included is None or node.id in included)
    ]
    internal_ids = {node.id for node in internal}
    nodes = [_node_copy(node, step.id, step.input) for node in internal]
    edges: list[dict[str, str]] = []
    for edge in definition.spec.edges:
        if edge.source in internal_ids and edge.target in internal_ids:
            edges.append(
                {
                    "source": f"{step.id}__{edge.source}",
                    "target": f"{step.id}__{edge.target}",
                    **({"when": edge.when} if edge.when is not None else {}),
                }
            )
        elif (
            edge.source not in internal_ids
            and edge.target in internal_ids
            and next(
                node.type
                for node in definition.spec.nodes
                if node.id == edge.source
            )
            == NodeType.INPUT
        ):
            edges.append({"source": "input", "target": f"{step.id}__{edge.target}"})
    if output_mapping is None:
        output_node = next(
            node for node in definition.spec.nodes if node.type == NodeType.OUTPUT
        )
        output_mapping = output_node.input
    result_input = _replace_refs(output_mapping, step.input, f"{step.id}__")
    nodes.append(
        {
            "id": step.id,
            "type": "transform",
            "displayName": f"{step.id} 结果",
            "input": result_input,
            "config": {"operation": "passthrough"},
        }
    )
    output_sources = {
        match.group("root")
        for match in _REF.finditer(json.dumps(result_input, ensure_ascii=False))
        if match.group("root") != "input"
    }
    fallback_only = {
        node.fallback.target for node in internal if node.fallback is not None
    }
    for source in sorted(output_sources - {f"{step.id}__{item}" for item in fallback_only}):
        edges.append({"source": source, "target": step.id})
    if not output_sources:
        edges.append({"source": "input", "target": step.id})
    return nodes, edges, step.id


def _fragment_step_inputs(data: dict[str, Any], step: PlanStep) -> dict[str, Any]:
    mapping = data.get("inputMapping")
    if not isinstance(mapping, dict):
        raise PlanCompileError(f"fragment 步骤 {step.id} 缺少冻结 inputMapping")
    return _replace_refs(mapping, step.input, "")


def _required_inputs(schema: dict[str, Any], mapping: dict[str, Any], step_id: str) -> None:
    missing = set(schema.get("required", [])) - mapping.keys()
    if missing:
        raise PlanCompileError(f"步骤 {step_id} 缺少必填输入: {sorted(missing)}")


def _property_type(schema: dict[str, Any], path: str) -> str | None:
    current = schema
    for segment in path.split("."):
        properties = current.get("properties", {})
        current = properties.get(segment, {}) if isinstance(properties, dict) else {}
        if not isinstance(current, dict):
            return None
    value = current.get("type")
    return value if isinstance(value, str) else None


def _validate_mapping_types(
    mapping: dict[str, Any],
    target_schema: dict[str, Any],
    source_schemas: dict[str, dict[str, Any]],
    step_id: str,
) -> None:
    target_properties = target_schema.get("properties", {})
    for target_field, value in mapping.items():
        if not isinstance(value, str):
            continue
        match = re.fullmatch(r"\$\{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>[^}]+)}", value)
        if match is None:
            continue
        source_schema = source_schemas.get(match.group("root"))
        if source_schema is None:
            raise PlanCompileError(
                f"步骤 {step_id} 引用了尚不可用的源 {match.group('root')}"
            )
        source_type = _property_type(source_schema, match.group("path"))
        target_definition = (
            target_properties.get(target_field, {})
            if isinstance(target_properties, dict)
            else {}
        )
        target_type = (
            target_definition.get("type")
            if isinstance(target_definition, dict)
            else None
        )
        if source_type and target_type and source_type != target_type:
            raise PlanCompileError(
                f"步骤 {step_id}.{target_field} 类型 {target_type} "
                f"不可接收 {match.group('root')}.{match.group('path')} 的 {source_type}"
            )


def _direct_step(step: PlanStep) -> dict[str, Any]:
    if step.kind == "transform":
        return {
            "id": step.id,
            "type": "transform",
            "displayName": step.config.get("displayName", step.id),
            "input": _replace_plan_refs(step.input),
            "config": step.config,
        }
    output_schema = step.config.get("outputSchema")
    if not isinstance(output_schema, dict):
        raise PlanCompileError(f"{step.kind} 步骤必须声明 outputSchema")
    requested = set(step.config.get("allowedCapabilities", []))
    granted = set(get_settings().agent_host_capabilities)
    if requested - granted:
        raise PlanCompileError(
            f"{step.kind} 步骤请求未授权能力: {sorted(requested - granted)}"
        )
    return {
        "id": step.id,
        "type": step.kind,
        "displayName": step.config.get("displayName", step.id),
        "input": _replace_plan_refs(step.input),
        "config": {
            **step.config,
            "outputSchema": output_schema,
            "maxSteps": step.config.get("maxSteps", 4),
            "allowedCapabilities": step.config.get("allowedCapabilities", []),
        },
    }


class PlanCompiler:
    async def compile(
        self, session: AsyncSession, raw_plan: dict[str, Any]
    ) -> tuple[
        ExecutionPlan,
        dict[str, Any],
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[str, Any],
    ]:
        try:
            plan = ExecutionPlan.model_validate(raw_plan)
        except Exception as exc:  # noqa: BLE001
            raise PlanCompileError(str(exc)) from exc
        step_ids = [step.id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            raise PlanCompileError("Plan step id 必须唯一")
        known = set(step_ids)
        for edge in plan.edges:
            if edge.source not in known or edge.target not in known:
                raise PlanCompileError("Plan edge 引用了未知步骤")

        nodes: list[dict[str, Any]] = [
            {"id": "input", "type": "input", "displayName": "计划输入"}
        ]
        edges: list[dict[str, Any]] = []
        source_refs: list[dict[str, Any]] = []
        locks: list[dict[str, Any]] = []
        source_map: dict[str, Any] = {
            "expandedToSource": {},
            "sourceToExpanded": {},
        }
        roots: dict[str, list[str]] = {}
        source_schemas: dict[str, dict[str, Any]] = {"input": plan.input_schema}
        for step in plan.steps:
            if step.kind == "workflow":
                if not step.ref:
                    raise PlanCompileError("workflow step 缺少 ref")
                manifest = await resolve_workflow_manifest(session, step.ref)
                version = await session.get(WorkflowVersion, manifest.workflow_version_id)
                if version is None or version.digest != manifest.workflow_digest:
                    raise PlanCompileError("workflow ref 版本锁漂移")
                definition = WorkflowDefinition.model_validate(version.definition_json)
                _required_inputs(definition.spec.input_schema, step.input, step.id)
                _validate_mapping_types(
                    step.input,
                    definition.spec.input_schema,
                    source_schemas,
                    step.id,
                )
                expanded, expanded_edges, _ = _expand_definition(definition, step)
                source_refs.append(
                    {"kind": "workflow", "ref": step.ref, "digest": version.digest}
                )
                locks.extend(
                    {
                        **lock,
                        "nodeId": f"{step.id}__{lock['nodeId']}",
                        "sourceRef": step.ref,
                    }
                    for lock in [
                        *version.validation_json.get("capabilityLocks", []),
                        *version.validation_json.get("mcpLocks", []),
                    ]
                )
                step_output_schema = definition.spec.output_schema
                source_node_ids = {
                    node.id
                    for node in definition.spec.nodes
                    if node.type not in {NodeType.INPUT, NodeType.OUTPUT}
                }
            elif step.kind == "fragment":
                if not step.ref:
                    raise PlanCompileError("fragment step 缺少 ref")
                fragment = await resolve_fragment_manifest(session, step.ref)
                version = await session.get(WorkflowVersion, fragment.workflow_version_id)
                if version is None or version.digest != fragment.source_workflow_digest:
                    raise PlanCompileError("fragment source 版本锁漂移")
                data = fragment.manifest_json
                _required_inputs(data["boundaryInputSchema"], step.input, step.id)
                _validate_mapping_types(
                    step.input,
                    data["boundaryInputSchema"],
                    source_schemas,
                    step.id,
                )
                definition = WorkflowDefinition.model_validate(version.definition_json)
                fragment_inputs = _fragment_step_inputs(data, step)
                output_mapping = data.get("outputMapping")
                if not output_mapping:
                    output_node = next(
                        node
                        for node in definition.spec.nodes
                        if node.type == NodeType.OUTPUT
                    )
                    output_mapping = output_node.input
                expanded, expanded_edges, _ = _expand_definition(
                    definition,
                    step.model_copy(update={"input": fragment_inputs}),
                    included=set(data["nodeIds"]),
                    output_mapping=output_mapping,
                )
                source_refs.append(
                    {
                        "kind": "fragment",
                        "ref": step.ref,
                        "digest": fragment.digest,
                        "closureDigest": fragment.closure_digest,
                    }
                )
                locks.extend(
                    {
                        **lock,
                        "nodeId": f"{step.id}__{lock['nodeId']}",
                        "sourceRef": step.ref,
                    }
                    for lock in [
                        *data.get("capabilityLocks", []),
                        *data.get("mcpLocks", []),
                    ]
                )
                step_output_schema = data["boundaryOutputSchema"]
                source_node_ids = set(data["nodeIds"])
            else:
                expanded = [_direct_step(step)]
                expanded_edges = [{"source": "input", "target": step.id}]
                step_output_schema = step.config.get("outputSchema", {})
                source_node_ids = set()
            nodes.extend(expanded)
            edges.extend(expanded_edges)
            for source_node_id in sorted(source_node_ids):
                expanded_id = f"{step.id}__{source_node_id}"
                source_map["expandedToSource"][expanded_id] = {
                    "stepId": step.id,
                    "sourceNodeId": source_node_id,
                    "sourceRef": step.ref,
                }
                source_map["sourceToExpanded"].setdefault(
                    f"{step.id}:{source_node_id}", []
                ).append(expanded_id)
            source_schemas[step.id] = step_output_schema
            roots[step.id] = [
                edge["target"] for edge in expanded_edges if edge["source"] == "input"
            ] or [step.id]
        for edge in plan.edges:
            for root in roots[edge.target]:
                candidate = {"source": edge.source, "target": root}
                if candidate not in edges:
                    edges.append(candidate)
        nodes.append(
            {
                "id": "output",
                "type": "output",
                "displayName": "计划输出",
                "input": _replace_plan_refs(plan.output),
            }
        )
        missing_outputs = set(plan.output_schema.get("required", [])) - plan.output.keys()
        if missing_outputs:
            raise PlanCompileError(
                f"计划输出缺少 required 字段: {sorted(missing_outputs)}"
            )
        _validate_mapping_types(
            plan.output, plan.output_schema, source_schemas, "output"
        )
        referenced = {
            match.group("root")
            for match in _REF.finditer(json.dumps(plan.output, ensure_ascii=False))
            if match.group("root") != "input"
        }
        for source in sorted(referenced):
            edges.append({"source": source, "target": "output"})
        definition = {
            "apiVersion": "workflow.platform/v1",
            "kind": "Workflow",
            "metadata": {"name": "ephemeral-plan", "ephemeral": True},
            "spec": {
                "inputSchema": plan.input_schema,
                "outputSchema": plan.output_schema,
                "nodes": nodes,
                "edges": edges,
                "policies": plan.policies,
            },
        }
        compiled = WorkflowCompiler().compile(definition)
        return (
            plan,
            compiled.definition.model_dump(by_alias=True, mode="json"),
            source_refs,
            locks,
            source_map,
        )


def plan_signature(
    plan: ExecutionPlan,
    source_refs: list[dict[str, Any]],
) -> str:
    def shape(value: Any) -> Any:
        if isinstance(value, str) and value.startswith("${"):
            return value
        if isinstance(value, dict):
            return {key: shape(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [shape(item) for item in value]
        return f"<{type(value).__name__}>"

    return digest_json(
        {
            "refs": source_refs,
            "steps": [{"kind": step.kind, "input": shape(step.input)} for step in plan.steps],
            "inputSchema": plan.input_schema,
            "outputSchema": plan.output_schema,
        }
    )


async def create_plan_snapshot(
    session: AsyncSession,
    raw_plan: dict[str, Any],
    *,
    created_by_conversation_run_id: str | None = None,
    ttl_hours: int = 168,
) -> PlanSnapshot:
    compiled = await compile_plan(session, raw_plan)
    plan = ExecutionPlan.model_validate(compiled["plan"])
    definition = compiled["definition"]
    source_refs = compiled["sourceRefs"]
    locks = compiled["locks"]
    source_map = compiled["sourceMap"]
    snapshot_payload = {
        "definition": definition,
        "sourceRefs": source_refs,
        "locks": locks,
        "sourceMap": source_map,
    }
    item = PlanSnapshot(
        id=str(uuid4()),
        definition_json=definition,
        digest=digest_json(snapshot_payload),
        source_refs_json=source_refs,
        locks_json=locks,
        source_map_json=source_map,
        created_by_conversation_run_id=created_by_conversation_run_id,
        expires_at=utcnow() + timedelta(hours=max(1, ttl_hours)),
        status="active",
        plan_signature=plan_signature(plan, source_refs),
    )
    session.add(item)
    await session.flush()
    return item


async def compile_plan(
    session: AsyncSession,
    raw_plan: dict[str, Any],
) -> dict[str, Any]:
    """Compile a plan without persisting a snapshot."""

    plan, definition, source_refs, locks, source_map = await PlanCompiler().compile(
        session, raw_plan
    )
    payload = {
        "plan": plan.model_dump(by_alias=True, mode="json"),
        "definition": definition,
        "sourceRefs": source_refs,
        "locks": locks,
        "sourceMap": source_map,
    }
    return {
        **payload,
        "digest": digest_json(payload),
        "planSignature": plan_signature(plan, source_refs),
    }


def verify_plan_snapshot(snapshot: PlanSnapshot, *, for_new_run: bool = True) -> None:
    if snapshot.status not in {"active", "expired", "retired"}:
        raise CatalogError("PlanSnapshot 状态非法")
    expires_at = snapshot.expires_at
    if expires_at.tzinfo is None:
        from datetime import UTC

        expires_at = expires_at.replace(tzinfo=UTC)
    if for_new_run and (snapshot.status != "active" or expires_at <= utcnow()):
        raise CatalogError("PlanSnapshot 已过期或停用，不能创建新 Run")
    expected = digest_json(
        {
            "definition": snapshot.definition_json,
            "sourceRefs": snapshot.source_refs_json,
            "locks": snapshot.locks_json,
            "sourceMap": snapshot.source_map_json,
        }
    )
    if not snapshot.source_map_json:
        expected = digest_json(
            {
                "definition": snapshot.definition_json,
                "sourceRefs": snapshot.source_refs_json,
                "locks": snapshot.locks_json,
            }
        )
    if expected != snapshot.digest:
        raise CatalogError("PlanSnapshot digest 不一致")


def expire_plan_snapshot(snapshot: PlanSnapshot) -> bool:
    expires_at = snapshot.expires_at
    if expires_at.tzinfo is None:
        from datetime import UTC

        expires_at = expires_at.replace(tzinfo=UTC)
    if snapshot.status == "active" and expires_at <= utcnow():
        snapshot.status = "expired"
        return True
    return False
