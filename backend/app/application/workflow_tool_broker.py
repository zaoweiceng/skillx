from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.compiler import WorkflowValidationError, digest_json
from app.application.plans import PlanCompileError, compile_plan
from app.application.tool_broker import ToolBrokerError, ToolExecutionContext
from app.application.workflow_catalog import (
    CatalogError,
    consume_session_call,
    create_private_fragment_snapshot,
    describe_fragment,
    describe_workflow,
    disclose_refs,
    record_disclosure_failure,
    record_disclosure_response,
    require_catalog_action,
    require_disclosed_ref,
    require_plan_refs_disclosed,
    resolve_workflow_manifest,
    search_manifests,
)
from app.domain.workflow import NodeType, WorkflowDefinition
from app.infrastructure.database import WorkflowVersion


class _WorkflowToolBase:
    effect = "none"
    idempotent = True
    fixture_policy = "do_not_capture"
    output_schema = {"type": "object"}

    def __init__(self, session: AsyncSession, disclosure_session_id: str) -> None:
        self.session = session
        self.disclosure_session_id = disclosure_session_id


class WorkflowSearchAdapter(_WorkflowToolBase):
    capability_ref = "workflow.search"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "constraints": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        call = await consume_session_call(
            self.session,
            self.disclosure_session_id,
            "search",
            {
                "queryDigest": digest_json(arguments["query"]),
                "limit": arguments.get("limit", 5),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
        )
        items = await search_manifests(
            self.session,
            arguments["query"],
            constraints=arguments.get("constraints"),
            limit=arguments.get("limit", 5),
        )
        await disclose_refs(
            self.session,
            self.disclosure_session_id,
            [str(item["workflowRef"]) for item in items],
            "L0",
        )
        response = {"level": "L0", "count": len(items), "items": items}
        await record_disclosure_response(self.session, call.id, response, level="L0")
        return response


class WorkflowDescribeAdapter(_WorkflowToolBase):
    capability_ref = "workflow.describe"
    input_schema = {
        "type": "object",
        "properties": {"ref": {"type": "string", "minLength": 1}},
        "required": ["ref"],
        "additionalProperties": False,
    }

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        ref = arguments["ref"]
        call = await consume_session_call(
            self.session,
            self.disclosure_session_id,
            "describe",
            {
                "refDigest": digest_json(ref),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
        )
        await require_disclosed_ref(self.session, self.disclosure_session_id, ref)
        result = await describe_workflow(self.session, ref)
        await disclose_refs(
            self.session,
            self.disclosure_session_id,
            [
                ref,
                *[
                    item["fragmentRef"]
                    for item in result.get("fragments", [])
                    if item.get("fragmentRef")
                ],
            ],
            "L1",
        )
        response = {"level": "L1", "workflow": result}
        await record_disclosure_response(self.session, call.id, response, level="L1")
        return response


class WorkflowDescribeFragmentAdapter(_WorkflowToolBase):
    capability_ref = "workflow.describe_fragment"
    input_schema = WorkflowDescribeAdapter.input_schema

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        ref = arguments["ref"]
        call = await consume_session_call(
            self.session,
            self.disclosure_session_id,
            "describe",
            {
                "refDigest": digest_json(ref),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
        )
        await require_disclosed_ref(self.session, self.disclosure_session_id, ref)
        response = {"level": "L2", "fragment": await describe_fragment(self.session, ref)}
        await disclose_refs(self.session, self.disclosure_session_id, [ref], "L2")
        await record_disclosure_response(self.session, call.id, response, level="L2")
        return response


class WorkflowSliceAdapter(_WorkflowToolBase):
    capability_ref = "workflow.slice"
    input_schema = {
        "type": "object",
        "properties": {
            "workflowRef": {"type": "string", "minLength": 1},
            "intent": {"type": "string", "minLength": 1, "maxLength": 500},
            "candidateNodeIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
        },
        "required": ["workflowRef", "intent"],
        "additionalProperties": False,
    }

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        workflow_ref = arguments["workflowRef"]
        await require_disclosed_ref(
            self.session, self.disclosure_session_id, workflow_ref
        )
        candidate_ids = list(arguments.get("candidateNodeIds") or [])
        if not candidate_ids:
            candidate_ids = await self._select_by_summary(
                workflow_ref, arguments["intent"]
            )
        call = await consume_session_call(
            self.session,
            self.disclosure_session_id,
            "slice",
            {
                "workflowRefDigest": digest_json(workflow_ref),
                "intentDigest": digest_json(arguments["intent"]),
                "candidateNodeIds": sorted(candidate_ids),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
        )
        snapshot = await create_private_fragment_snapshot(
            self.session, workflow_ref, candidate_ids
        )
        await disclose_refs(
            self.session,
            self.disclosure_session_id,
            [snapshot.fragment_ref],
            "L2",
        )
        response = {
            "level": "L2",
            "fragment": {**snapshot.manifest_json, "manifestDigest": snapshot.digest},
        }
        await record_disclosure_response(self.session, call.id, response, level="L2")
        return response

    async def _select_by_summary(self, workflow_ref: str, intent: str) -> list[str]:
        manifest = await resolve_workflow_manifest(self.session, workflow_ref)
        version = await self.session.get(WorkflowVersion, manifest.workflow_version_id)
        if version is None:
            raise CatalogError("WorkflowVersion 不存在")
        definition = WorkflowDefinition.model_validate(version.definition_json)
        candidates = [
            node
            for node in definition.spec.nodes
            if node.type not in {NodeType.INPUT, NodeType.OUTPUT, NodeType.APPROVAL}
            and not node.side_effect
        ]
        if not candidates:
            raise CatalogError("Workflow 没有可安全切片的节点")
        terms = {
            term
            for term in re.split(r"[\s,，。]+", intent.lower())
            if term
        }
        ranked = sorted(
            candidates,
            key=lambda node: sum(
                term in f"{node.display_name} {node.type.value}".lower()
                for term in terms
            ),
            reverse=True,
        )
        return [ranked[0].id]


class WorkflowCompilePlanAdapter(_WorkflowToolBase):
    capability_ref = "workflow.compile_plan"
    input_schema = {
        "type": "object",
        "properties": {"plan": {"type": "object"}},
        "required": ["plan"],
        "additionalProperties": False,
    }

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        plan = arguments["plan"]
        call = await consume_session_call(
            self.session,
            self.disclosure_session_id,
            "plan",
            {
                "planDigest": digest_json(plan),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
        )
        try:
            await require_plan_refs_disclosed(
                self.session, self.disclosure_session_id, plan
            )
            compiled = await compile_plan(self.session, plan)
            response = {
                "compiled": True,
                "compiledPlan": compiled,
                "digest": compiled["digest"],
                "diagnostics": [],
            }
        except (PlanCompileError, WorkflowValidationError, CatalogError) as exc:
            response = {
                "compiled": False,
                "diagnostics": [
                    {
                        "level": "error",
                        "code": "PLAN_COMPILATION_FAILED",
                        "message": str(exc),
                    }
                ],
            }
        await record_disclosure_response(self.session, call.id, response, level=None)
        return response


class WorkflowCatalogAdapter(_WorkflowToolBase):
    """One narrow descriptor; disclosure depth remains server controlled."""

    capability_ref = "workflow.catalog"
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "search",
                    "describe",
                    "describe_fragment",
                    "slice",
                    "compile_plan",
                ],
            },
            "query": {"type": "string", "minLength": 1, "maxLength": 2000},
            "constraints": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            "ref": {"type": "string", "minLength": 1},
            "workflowRef": {"type": "string", "minLength": 1},
            "intent": {"type": "string", "minLength": 1, "maxLength": 500},
            "candidateNodeIds": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 20,
            },
            "plan": {"type": "object"},
        },
        "required": ["action"],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"action": {"const": "search"}}},
                "then": {"required": ["query"]},
            },
            {
                "if": {
                    "properties": {
                        "action": {"enum": ["describe", "describe_fragment"]}
                    }
                },
                "then": {"required": ["ref"]},
            },
            {
                "if": {"properties": {"action": {"const": "slice"}}},
                "then": {"required": ["workflowRef", "intent"]},
            },
            {
                "if": {"properties": {"action": {"const": "compile_plan"}}},
                "then": {"required": ["plan"]},
            },
        ],
    }

    async def record_input_failure(
        self,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        code: str,
    ) -> None:
        action = str(arguments.get("action", "unknown"))
        operation = (
            "plan"
            if action == "compile_plan"
            else "describe"
            if action == "describe_fragment"
            else action
        )
        await record_disclosure_failure(
            self.session,
            self.disclosure_session_id,
            operation,
            {
                "action": action,
                "argumentsDigest": digest_json(arguments),
                "modelCallId": context.run_id,
                "toolCallId": context.call_id,
            },
            code=code,
        )
        await self.session.commit()

    async def execute(
        self, context: ToolExecutionContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        action = str(arguments["action"])
        operation = (
            "plan"
            if action == "compile_plan"
            else "describe"
            if action == "describe_fragment"
            else action
        )
        summary = {
            "action": action,
            "argumentsDigest": digest_json(
                {key: value for key, value in arguments.items() if key != "action"}
            ),
            "modelCallId": context.run_id,
            "toolCallId": context.call_id,
        }
        adapters: dict[str, _WorkflowToolBase] = {
            "search": WorkflowSearchAdapter(self.session, self.disclosure_session_id),
            "describe": WorkflowDescribeAdapter(self.session, self.disclosure_session_id),
            "describe_fragment": WorkflowDescribeFragmentAdapter(
                self.session, self.disclosure_session_id
            ),
            "slice": WorkflowSliceAdapter(self.session, self.disclosure_session_id),
            "compile_plan": WorkflowCompilePlanAdapter(
                self.session, self.disclosure_session_id
            ),
        }
        payload = {key: value for key, value in arguments.items() if key != "action"}
        try:
            await require_catalog_action(
                self.session, self.disclosure_session_id, action
            )
            output = await adapters[action].execute(context, payload)
            await self.session.commit()
            return {"action": action, **output}
        except CatalogError as exc:
            message = str(exc)
            if "第一个 action" in message:
                code = "CATALOG_STATE_ORDER_INVALID"
            elif "未由当前会话" in message:
                code = "CATALOG_REF_NOT_DISCLOSED"
            elif "超过上限" in message:
                code = "CATALOG_CALL_BUDGET_EXCEEDED"
            else:
                code = "CATALOG_CONTRACT_INVALID"
            await record_disclosure_failure(
                self.session,
                self.disclosure_session_id,
                operation,
                summary,
                code=code,
            )
            await self.session.commit()
            raise ToolBrokerError(code, message) from exc


def workflow_tool_adapters(
    session: AsyncSession, disclosure_session_id: str
) -> list[_WorkflowToolBase]:
    return [
        WorkflowSearchAdapter(session, disclosure_session_id),
        WorkflowDescribeAdapter(session, disclosure_session_id),
        WorkflowDescribeFragmentAdapter(session, disclosure_session_id),
        WorkflowSliceAdapter(session, disclosure_session_id),
        WorkflowCompilePlanAdapter(session, disclosure_session_id),
    ]


def workflow_catalog_adapter(
    session: AsyncSession, disclosure_session_id: str
) -> WorkflowCatalogAdapter:
    return WorkflowCatalogAdapter(session, disclosure_session_id)
