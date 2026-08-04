from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.compiler import digest_json
from app.application.expressions import ExpressionError, resolve_mapping
from app.application.security import redact
from app.config import get_settings
from app.domain.workflow import NON_RECOVERABLE_ERROR_CODES, NodeType, WorkflowDefinition
from app.infrastructure.database import (
    Approval,
    Artifact,
    ExecutionCheckpoint,
    NodeRun,
    OutputSubstitution,
    RecoveryAttempt,
    Run,
    utcnow,
)

# 006-A/B：可执行 ExecutionCheckpoint 与真正把上下文交给受限恢复 Agent。
#
# 恢复只消费持久化 checkpoint（已完成输出**值**、失败输入/完整错误、能力锁、剩余子图的
# 节点定义与边、已提交副作用/幂等键），不依赖原 Worker 内存，也不重新猜测已完成步骤。
# 原失败 NodeRun 永远不可变：恢复通过 OutputSubstitution 提供替代输出，并新建唯一
# Recovery NodeRun 执行 Agent；已完成的非幂等副作用绝不重放；无法证明结果时转人工确认。


# RecoveryDecision 判别联合：action ∈
# provide_substitution | retry_replanned | request_user | abort。
# 每种 action 禁止额外字段（provide_substitution 必须带 substitution，其它不得带）。
RECOVERY_ACTIONS = ("provide_substitution", "retry_replanned", "request_user", "abort")
RECOVERY_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(RECOVERY_ACTIONS)},
        "substitution": {"type": "object"},
        "planPatch": {
            "type": "object",
            "properties": {
                "remainingNodes": {"type": "array", "items": {"type": "object"}},
                "remainingEdges": {"type": "array", "items": {"type": "object"}},
                "inputMappings": {"type": "object"},
                "locks": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["remainingNodes", "remainingEdges", "inputMappings", "locks"],
            "additionalProperties": False,
        },
        "question": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


class RecoveryPlanPatch(BaseModel):
    remaining_nodes: list[dict[str, Any]] = Field(alias="remainingNodes")
    remaining_edges: list[dict[str, Any]] = Field(alias="remainingEdges")
    input_mappings: dict[str, Any] = Field(alias="inputMappings")
    locks: list[dict[str, Any]]

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class RecoveryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _fallback_only_targets(definition: WorkflowDefinition) -> set[str]:
    """所有被声明为 fallback.target 的节点 id：这些是 fallback-only 恢复目标，
    不应出现在 normal 剩余子图里。"""

    return {
        node.fallback.target
        for node in definition.spec.nodes
        if node.fallback is not None
    }


async def build_checkpoint(
    session: AsyncSession,
    run: Run,
    definition: WorkflowDefinition,
    failed_node_run: NodeRun,
) -> ExecutionCheckpoint:
    """冻结失败点的可执行恢复上下文为持久化 ExecutionCheckpoint。

    保存：已完成节点的结构化输出**值**、失败节点解析输入/完整标准错误/重试历史、能力锁、
    从已发布 DAG 计算的剩余节点定义与边、workflow 输出契约、已提交（非幂等已完成）副作用
    与幂等键，以及 fallback target Agent 的受限配置。
    """

    nodes = {node.id: node for node in definition.spec.nodes}
    failed_node = nodes.get(failed_node_run.node_id)
    fallback = failed_node.fallback if failed_node else None
    fallback_config: dict[str, Any] = {}
    if fallback is not None:
        target = nodes.get(fallback.target)
        if target is not None:
            fallback_config = {
                "target": fallback.target,
                "allowedCapabilities": target.config.get("allowedCapabilities", []),
                "maxSteps": target.config.get("maxSteps", 4),
                "maxTotalTokens": target.config.get("maxTotalTokens", 20000),
                "requireUserApproval": fallback.require_user_approval,
            }

    latest = await _latest_node_runs(session, run.id)
    artifact_rows = (
        await session.scalars(select(Artifact).where(Artifact.run_id == run.id))
    ).all()
    artifacts_by_node: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifact_rows:
        node_id = (artifact.metadata_json or {}).get("nodeId")
        if node_id:
            artifacts_by_node.setdefault(str(node_id), []).append(
                {
                    "artifactId": artifact.id,
                    "uri": artifact.uri,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                    "mediaType": artifact.media_type,
                }
            )
    substitutions = await load_substitutions(session, run.id)
    completed_outputs: dict[str, Any] = {}
    committed_side_effects: list[dict[str, Any]] = []
    for node_id, node_run in latest.items():
        if node_run.status == "succeeded":
            completed_outputs[node_id] = _checkpoint_output(
                node_run, artifacts_by_node.get(node_id, [])
            )
            node = nodes.get(node_id)
            if node is not None and node.side_effect:
                committed_side_effects.append(
                    {
                        "nodeId": node_id,
                        "idempotent": node.idempotent,
                        # 幂等键：非幂等副作用不重放，据此识别“已提交”。
                        "idempotencyKey": f"{run.id}:{node_id}:{node_run.attempt}",
                    }
                )
    # 已有替代输出的节点也视为已解析（供多轮恢复引用）。
    for node_id, output in substitutions.items():
        completed_outputs.setdefault(node_id, output)

    fallback_only = _fallback_only_targets(definition)
    # remaining 子图：未解析、非 input/output、且非 fallback-only target 的节点定义与边。
    remaining_nodes = [
        node.model_dump(by_alias=True, mode="json")
        for node in definition.spec.nodes
        if node.id not in completed_outputs
        and node.type not in (NodeType.INPUT, NodeType.OUTPUT)
        and node.id not in fallback_only
    ]
    remaining_ids = {node["id"] for node in remaining_nodes} | {failed_node_run.node_id}
    remaining_edges = [
        {"source": edge.source, "target": edge.target}
        for edge in definition.spec.edges
        if edge.target in remaining_ids or edge.source in remaining_ids
    ]

    retry_history = [
        {"attempt": item.attempt, "status": item.status, "error": item.error_json}
        for item in await _node_attempts(session, run.id, failed_node_run.node_id)
    ]
    error = failed_node_run.error_json or {}
    from app.application.orchestrator import load_execution_source

    source = await load_execution_source(session, run)
    snapshot = {
        "workflowVersionId": run.workflow_version_id,
        "planSnapshotId": run.plan_snapshot_id,
        "completedOutputs": completed_outputs,
        "committedSideEffects": committed_side_effects,
        "failedNode": {
            "nodeId": failed_node_run.node_id,
            "type": failed_node_run.node_type,
            "input": failed_node_run.input_json,
            "error": error,
            "attempt": failed_node_run.attempt,
            "retryHistory": retry_history,
            # outcome certainty：非确定结果（未知副作用）需人工确认。
            "outcomeCertainty": (
                "unknown" if error.get("code") == "UNKNOWN_OUTCOME" else "known"
            ),
            "outputSchema": (failed_node.config.get("outputSchema") if failed_node else None),
        },
        "remainingNodes": remaining_nodes,
        "remainingEdges": remaining_edges,
        "remainingSubgraph": sorted(node["id"] for node in remaining_nodes),
        "outputContract": definition.spec.output_schema,
        "capabilityLocks": (
            source.validation_json.get("capabilityLocks", []) if source else []
        ),
        "fallbackConfig": fallback_config,
        "runInput": run.input_json,
    }
    approval_status = "pending" if fallback_config.get("requireUserApproval") else "none"
    checkpoint = ExecutionCheckpoint(
        id=str(uuid4()),
        run_id=run.id,
        workflow_version_id=(
            run.workflow_version_id or f"plan:{run.plan_snapshot_id}"
        ),
        failed_node_id=failed_node_run.node_id,
        failed_node_run_id=failed_node_run.id,
        attempt=failed_node_run.attempt,
        digest=digest_json(snapshot),
        status="pending",
        approval_status=approval_status,
        snapshot_json=snapshot,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint

def build_recovery_prompt(checkpoint: ExecutionCheckpoint) -> str:
    """由平台模板 + checkpoint 摘要生成 Recovery Agent prompt。

    真正把上下文交给 Agent：包含失败事实、失败输入、**已完成节点的输出值**（脱敏）、
    剩余子图与 workflow 输出契约，而不仅是节点 id 列表。绝不拼整库或原始私有轨迹。
    """

    snapshot = checkpoint.snapshot_json
    failed = snapshot.get("failedNode", {})
    context = {
        "failedNode": {
            "nodeId": failed.get("nodeId"),
            "type": failed.get("type"),
            "errorCode": (failed.get("error") or {}).get("code"),
            "input": redact(failed.get("input")),
            "outputSchema": failed.get("outputSchema"),
            "outcomeCertainty": failed.get("outcomeCertainty"),
        },
        # 已完成节点的实际输出值（脱敏）：Agent 可据此计算替代输出。
        "completedOutputs": redact(snapshot.get("completedOutputs", {})),
        "remainingSubgraph": snapshot.get("remainingSubgraph", []),
        "remainingNodes": snapshot.get("remainingNodes", []),
        "remainingEdges": snapshot.get("remainingEdges", []),
        "capabilityLocks": snapshot.get("capabilityLocks", []),
        "outputContract": snapshot.get("outputContract"),
        "userAnswer": redact(snapshot.get("userAnswer")),
        "runInput": redact(snapshot.get("runInput", {})),
    }
    context = _bounded_recovery_context(context)
    return (
        "你是受限恢复 Agent。已发布工作流的某节点重试耗尽，请基于以下 checkpoint 上下文"
        "决定如何恢复。只能返回 RecoveryDecision，action ∈ "
        "[provide_substitution, retry_replanned, request_user, abort]。"
        "provide_substitution 时必须给出 substitution 对象作为失败节点的替代输出，"
        "并应复用 completedOutputs 中的已完成值；已完成的非幂等副作用不得重放；"
        "outcomeCertainty 为 unknown 时选择 request_user。\n"
        f"context: {json.dumps(context, ensure_ascii=False)}"
    )


def _checkpoint_output(
    node_run: NodeRun, artifact_refs: list[dict[str, Any]]
) -> Any:
    value = node_run.output_json
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    if len(encoded) <= 16_384:
        return value
    return {
        "summary": {
            "type": type(value).__name__,
            "bytes": len(encoded),
            "digest": digest_json(value),
        },
        "nodeRunRef": node_run.id,
        "artifactRefs": artifact_refs,
    }


def _bounded_recovery_context(context: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True).encode()
    if len(encoded) <= 24_000:
        return context
    output_contract = context.get("outputContract", {})
    properties = (
        output_contract.get("properties", {})
        if isinstance(output_contract, dict)
        else {}
    )
    return {
        **context,
        "failedNode": {
            **context.get("failedNode", {}),
            "input": _prompt_summary(context.get("failedNode", {}).get("input")),
        },
        "completedOutputs": {
            node_id: (
                value
                if isinstance(value, dict) and "summary" in value
                else {"summary": {"digest": digest_json(value)}}
            )
            for node_id, value in context.get("completedOutputs", {}).items()
        },
        "outputContract": {
            "required": output_contract.get("required", [])
            if isinstance(output_contract, dict)
            else [],
            "properties": {
                key: {"type": item.get("type", "unknown")}
                for key, item in properties.items()
                if isinstance(item, dict)
            },
        },
        "runInput": _prompt_summary(context.get("runInput")),
        "contextTruncated": True,
    }


def _prompt_summary(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    if len(encoded) <= 4_096:
        return value
    return {
        "type": type(value).__name__,
        "bytes": len(encoded),
        "digest": digest_json(value),
    }


def parse_recovery_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """把 Agent 原始输出解析为规范化 RecoveryDecision，非法时 fail closed。

    - action 必须在允许集合内；
    - provide_substitution 必须带 substitution 对象，其它 action 不得带 substitution。
    """

    action = raw.get("action")
    if action not in RECOVERY_ACTIONS:
        raise RecoveryError("CONTRACT_VIOLATION", f"非法 RecoveryDecision action: {action}")
    substitution = raw.get("substitution")
    plan_patch = raw.get("planPatch")
    question = raw.get("question")
    if action == "provide_substitution":
        if not isinstance(substitution, dict):
            raise RecoveryError(
                "CONTRACT_VIOLATION", "provide_substitution 需要 substitution 对象"
            )
        if plan_patch is not None or question is not None:
            raise RecoveryError("CONTRACT_VIOLATION", "substitution 分支含非法字段")
    elif action == "retry_replanned":
        if substitution is not None or not isinstance(plan_patch, dict):
            raise RecoveryError(
                "CONTRACT_VIOLATION", "retry_replanned 需要 typed planPatch"
            )
        try:
            plan_patch = RecoveryPlanPatch.model_validate(plan_patch).model_dump(
                by_alias=True
            )
        except Exception as exc:  # noqa: BLE001
            raise RecoveryError("CONTRACT_VIOLATION", f"非法 planPatch: {exc}") from exc
    elif action == "request_user":
        if substitution is not None or plan_patch is not None or not question:
            raise RecoveryError(
                "CONTRACT_VIOLATION", "request_user 需要非空 question"
            )
    elif substitution is not None or plan_patch is not None or question is not None:
        raise RecoveryError(
            "CONTRACT_VIOLATION", f"{action} 携带了不允许的字段"
        )
    return {
        "action": action,
        "substitution": substitution,
        "planPatch": plan_patch,
        "question": question,
        "reason": raw.get("reason", ""),
    }


async def run_recovery_agent(
    checkpoint: ExecutionCheckpoint,
    fallback_config: dict[str, Any],
    *,
    host: AgentHost | None = None,
    cancellation: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """运行受限 Recovery Agent，返回校验过的 RecoveryDecision。

    Agent 只在 Host 边界内运行；调用携带持久 idempotency key（checkpoint id），
    输出必须匹配 RecoveryDecision Schema，否则 fail closed。
    """

    settings = get_settings()
    host = host or AgentHost()
    request = AgentHostRequest(
        run_id=f"recovery-{checkpoint.id}",
        prompt=build_recovery_prompt(checkpoint),
        output_schema=RECOVERY_DECISION_SCHEMA,
        working_directory=settings.artifact_root / "recovery" / checkpoint.id,
        requested_capabilities=tuple(fallback_config.get("allowedCapabilities", [])),
        max_steps=int(fallback_config.get("maxSteps", 4)),
        max_total_tokens=int(fallback_config.get("maxTotalTokens", 20000)),
        max_duration_seconds=int(fallback_config.get("maxDurationSeconds", 60)),
    )
    try:
        result = await host.execute(request, cancellation=cancellation)
    except AgentHostError as exc:
        raise RecoveryError(exc.code, exc.message) from exc
    return parse_recovery_decision(result.output)


def is_recoverable(error_code: str | None, fallback_on: list[str]) -> bool:
    """判断某错误码是否允许自动进入恢复。

    - 必须在 fallback.on 声明的错误码集合内；
    - 安全/契约/未知类错误默认不自动恢复（由 compiler 校验保证需人工审批）。
    """

    if error_code is None or error_code not in fallback_on:
        return False
    return error_code not in NON_RECOVERABLE_ERROR_CODES


async def _latest_node_runs(session: AsyncSession, run_id: str) -> dict[str, NodeRun]:
    rows = (
        await session.scalars(
            select(NodeRun)
            .where(NodeRun.run_id == run_id)
            .order_by(NodeRun.node_id, NodeRun.attempt.desc())
        )
    ).all()
    latest: dict[str, NodeRun] = {}
    for row in rows:
        latest.setdefault(row.node_id, row)
    return latest


async def _node_attempts(
    session: AsyncSession, run_id: str, node_id: str
) -> list[NodeRun]:
    return list(
        (
            await session.scalars(
                select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
                .order_by(NodeRun.attempt)
            )
        ).all()
    )


async def load_substitutions(session: AsyncSession, run_id: str) -> dict[str, Any]:
    """加载某 Run 的所有替代输出（node_id → output），供输入解析与调度读取。"""

    rows = (
        await session.scalars(
            select(OutputSubstitution).where(OutputSubstitution.run_id == run_id)
        )
    ).all()
    return {row.node_id: row.output_json for row in rows}


def verify_checkpoint(checkpoint: ExecutionCheckpoint) -> None:
    """恢复前再次核验 checkpoint digest 固定内容未被篡改。"""

    expected = digest_json(checkpoint.snapshot_json)
    if expected != checkpoint.digest:
        raise RecoveryError("CHECKPOINT_DRIFT", "ExecutionCheckpoint digest 与内容不一致")


def _validate_substitution(checkpoint: ExecutionCheckpoint, output: dict[str, Any]) -> None:
    """用失败节点 outputSchema（若声明）校验替代输出，类型不匹配 fail closed。"""

    schema = (checkpoint.snapshot_json.get("failedNode") or {}).get("outputSchema")
    if isinstance(schema, dict):
        errors = list(Draft202012Validator(schema).iter_errors(output))
        if errors:
            raise RecoveryError(
                "CONTRACT_VIOLATION",
                f"替代输出不符合失败节点 outputSchema: {errors[0].message}",
            )


async def apply_recovery_decision(
    session: AsyncSession,
    checkpoint: ExecutionCheckpoint,
    decision: dict[str, Any],
) -> str:
    """应用 RecoveryDecision，返回运行的后续状态提示。

    - provide_substitution：把 Agent 提供的替代输出写入 OutputSubstitution（**不覆盖**失败
      NodeRun），从剩余子图继续；已成功 NodeRun 不重建，已完成非幂等副作用不重放。
    - retry_replanned：不提供替代输出，记录 skip 型替代（output={}）；下游若仍依赖失败节点
      输出则输入解析失败并 fail closed。
    - request_user：转人工确认。
    - abort：终止为失败。
    """

    verify_checkpoint(checkpoint)
    decision = parse_recovery_decision(decision)
    action = decision["action"]
    run = await session.get(Run, checkpoint.run_id)
    if run is None:
        raise RecoveryError("RUN_NOT_FOUND", "恢复目标 Run 不存在")
    checkpoint.decision_json = _redacted_decision(decision)
    checkpoint.status = "applied"

    if action == "abort":
        run.status = "failed"
        run.finished_at = utcnow()
        run.error_json = {
            "code": "RECOVERY_ABORTED",
            "message": decision.get("reason") or "恢复决定终止运行",
        }
        checkpoint.status = "aborted"
        await session.flush()
        return "failed"
    if action == "request_user":
        question = decision["question"]
        approval_snapshot = {
            "runId": run.id,
            "nodeId": checkpoint.failed_node_id,
            "title": "恢复需要用户输入",
            "description": question,
            "payload": {"checkpointId": checkpoint.id, "question": question},
            "effect": "recovery.question",
            "checkpointId": checkpoint.id,
        }
        session.add(
            Approval(
                id=str(uuid4()),
                run_id=run.id,
                node_run_id=checkpoint.failed_node_run_id,
                node_id=checkpoint.failed_node_id,
                snapshot_digest=digest_json(approval_snapshot),
                snapshot_json=approval_snapshot,
                status="pending",
            )
        )
        run.status = "waiting_approval"
        checkpoint.status = "awaiting_user"
        await session.flush()
        return "waiting_approval"
    if action == "provide_substitution":
        output = decision["substitution"]
        _validate_substitution(checkpoint, output)
        await _record_substitution(session, checkpoint, output, source="recovery")
    else:  # retry_replanned
        plan_patch = RecoveryPlanPatch.model_validate(decision["planPatch"])
        _validate_recovery_plan_patch(checkpoint, plan_patch)
        existing = await session.scalar(
            select(RecoveryAttempt)
            .where(RecoveryAttempt.checkpoint_id == checkpoint.id)
            .order_by(RecoveryAttempt.attempt.desc())
        )
        attempt = (existing.attempt if existing else 0) + 1
        payload = plan_patch.model_dump(by_alias=True)
        session.add(
            RecoveryAttempt(
                id=str(uuid4()),
                checkpoint_id=checkpoint.id,
                attempt=attempt,
                plan_patch_json=payload,
                digest=digest_json(payload),
                status="active",
            )
        )
        checkpoint.snapshot_json = {
            **checkpoint.snapshot_json,
            "remainingNodes": payload["remainingNodes"],
            "remainingEdges": payload["remainingEdges"],
            "recoveryInputMappings": payload["inputMappings"],
        }
        checkpoint.digest = digest_json(checkpoint.snapshot_json)
        latest_runs = await _latest_node_runs(session, run.id)
        completed_values = {
            node_id: item.output_json
            for node_id, item in latest_runs.items()
            if item.status == "succeeded" and item.output_json is not None
        }
        failed_mapping = payload["inputMappings"].get(checkpoint.failed_node_id)
        try:
            retry_input = (
                resolve_mapping(failed_mapping, run.input_json, completed_values)
                if isinstance(failed_mapping, dict)
                else checkpoint.snapshot_json["failedNode"]["input"]
            )
        except ExpressionError as exc:
            raise RecoveryError(
                "RECOVERY_PLAN_INVALID", f"失败节点 input mapping 无法解析: {exc}"
            ) from exc
        latest_failed_attempt = max(
            (
                item.attempt
                for item in latest_runs.values()
                if item.node_id == checkpoint.failed_node_id
            ),
            default=checkpoint.attempt,
        )
        failed_definition = next(
            node
            for node in payload["remainingNodes"]
            if node["id"] == checkpoint.failed_node_id
        )
        await session.execute(
            sqlite_insert(NodeRun)
            .values(
                id=str(uuid4()),
                run_id=run.id,
                node_id=checkpoint.failed_node_id,
                node_type=failed_definition["type"],
                attempt=latest_failed_attempt + 1,
                status="ready",
                input_json=retry_input,
                available_at=utcnow(),
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=[NodeRun.run_id, NodeRun.node_id, NodeRun.attempt]
            )
        )
    # 让 Run 回到 running，由 Orchestrator 从剩余子图继续调度。
    run.status = "running"
    run.error_json = None
    await session.flush()
    return "running"


def _redacted_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": decision["action"],
        "reason": decision.get("reason", ""),
        "substitution": redact(decision.get("substitution"))
        if decision.get("substitution") is not None
        else None,
        "planPatch": redact(decision.get("planPatch")),
        "question": decision.get("question"),
    }


def _validate_recovery_plan_patch(
    checkpoint: ExecutionCheckpoint, patch: RecoveryPlanPatch
) -> None:
    snapshot = checkpoint.snapshot_json
    allowed_nodes = {
        node["id"] for node in snapshot.get("remainingNodes", []) if "id" in node
    }
    completed_nodes = set(snapshot.get("completedOutputs", {}))
    proposed_nodes = {node.get("id") for node in patch.remaining_nodes}
    if None in proposed_nodes or proposed_nodes != allowed_nodes:
        raise RecoveryError(
            "RECOVERY_PLAN_INVALID", "replan 必须完整覆盖且不得扩大 remaining nodes"
        )
    for edge in patch.remaining_edges:
        if edge.get("source") not in proposed_nodes | completed_nodes | {"input"} or edge.get(
            "target"
        ) not in proposed_nodes | {"output"}:
            raise RecoveryError("RECOVERY_PLAN_INVALID", "replan edge 越过剩余子图边界")
    if not any(edge.get("target") == "output" for edge in patch.remaining_edges):
        raise RecoveryError("RECOVERY_PLAN_INVALID", "replan 必须保留最终 output 边界")
    if not set(patch.input_mappings) <= proposed_nodes:
        raise RecoveryError("RECOVERY_PLAN_INVALID", "inputMappings 含剩余子图外节点")
    allowed_locks = {
        digest_json(lock) for lock in snapshot.get("capabilityLocks", [])
    }
    if any(digest_json(lock) not in allowed_locks for lock in patch.locks):
        raise RecoveryError("RECOVERY_PLAN_INVALID", "replan 不得扩大能力锁")


async def _record_substitution(
    session: AsyncSession,
    checkpoint: ExecutionCheckpoint,
    output: dict[str, Any],
    *,
    source: str,
) -> None:
    """记录失败节点的替代输出（不可覆盖失败 NodeRun）。"""

    await session.execute(
        sqlite_insert(OutputSubstitution)
        .values(
            id=str(uuid4()),
            run_id=checkpoint.run_id,
            checkpoint_id=checkpoint.id,
            node_id=checkpoint.failed_node_id,
            failed_node_run_id=checkpoint.failed_node_run_id,
            output_json=output,
            source=source,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(
            index_elements=[OutputSubstitution.run_id, OutputSubstitution.node_id]
        )
    )


class RecoveryDriver:
    """驱动 recovering 状态的 Run：原子领取 checkpoint、创建唯一 Recovery NodeRun、
    运行受限 Recovery Agent 并应用决定。

    A-02 原子领取：用单条 CAS 把 checkpoint ``pending→running`` 并写入租约，未领取成功的
    Worker 绝不调用 Agent；因此双 Worker 竞争只产生一次 Agent 调用。恢复只消费持久化
    checkpoint。
    """

    def __init__(self, host: AgentHost | None = None, worker_id: str = "recovery-driver") -> None:
        self._host = host
        self.worker_id = worker_id
        self.lease_seconds = get_settings().agent_lease_seconds

    async def poll_once(self) -> bool:
        from app.application.orchestrator import Orchestrator, append_event
        from app.infrastructure.database import SessionLocal

        now = utcnow()
        await self._reclaim_expired(now)
        # 1) 选候选并原子领取：只领取 run 仍 recovering、审批已通过（或无需审批）的 pending。
        async with SessionLocal() as session, session.begin():
            candidate = await session.scalar(
                select(ExecutionCheckpoint)
                .where(
                    ExecutionCheckpoint.status == "pending",
                    ExecutionCheckpoint.approval_status.in_(["none", "approved"]),
                )
                .order_by(ExecutionCheckpoint.created_at)
                .limit(1)
            )
            if candidate is None:
                return False
            run = await session.get(Run, candidate.run_id)
            if run is None or run.status != "recovering":
                candidate.status = "orphaned"
                return True
            checkpoint_id = candidate.id
            # 原子 CAS：只有把 pending→running 成功的 Worker 继续。
            claimed = await session.execute(
                update(ExecutionCheckpoint)
                .where(
                    ExecutionCheckpoint.id == checkpoint_id,
                    ExecutionCheckpoint.status == "pending",
                )
                .values(
                    status="running",
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    heartbeat_at=now,
                )
            )
            if claimed.rowcount != 1:
                return True  # 被其它 Worker 领取。
            fallback_config = candidate.snapshot_json.get("fallbackConfig", {})
            # 为 fallback target 创建唯一 Recovery NodeRun（供运行详情区分与幂等）。
            recovery_node_run_id = str(uuid4())
            target = fallback_config.get("target")
            if target:
                latest_attempt = await session.scalar(
                    select(NodeRun.attempt)
                    .where(
                        NodeRun.run_id == candidate.run_id,
                        NodeRun.node_id == target,
                    )
                    .order_by(NodeRun.attempt.desc())
                    .limit(1)
                )
                inserted = await session.execute(
                    sqlite_insert(NodeRun)
                    .values(
                        id=recovery_node_run_id,
                        run_id=candidate.run_id,
                        node_id=target,
                        node_type="agent",
                        attempt=(latest_attempt or 0) + 1,
                        status="running",
                        input_json={"checkpointId": checkpoint_id},
                        worker_id=self.worker_id,
                        available_at=now,
                        started_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[NodeRun.run_id, NodeRun.node_id, NodeRun.attempt]
                    )
                )
                if inserted.rowcount:
                    candidate.recovery_node_run_id = recovery_node_run_id
                    await append_event(
                        session,
                        candidate.run_id,
                        "recovery.attempt_started",
                        {"checkpointId": checkpoint_id, "target": target},
                        recovery_node_run_id,
                    )

        # 2) 运行 Recovery Agent（Host 边界内），不持有事务。
        try:
            async def cancelled() -> bool:
                async with SessionLocal() as heartbeat_session, heartbeat_session.begin():
                    current = await heartbeat_session.get(
                        ExecutionCheckpoint, checkpoint_id
                    )
                    if current is None:
                        return True
                    current_run = await heartbeat_session.get(Run, current.run_id)
                    if (
                        current.status != "running"
                        or current.lease_owner != self.worker_id
                        or current_run is None
                        or current_run.cancellation_requested_at is not None
                        or current_run.status == "cancelling"
                    ):
                        return True
                    current.heartbeat_at = utcnow()
                    current.lease_expires_at = utcnow() + timedelta(
                        seconds=self.lease_seconds
                    )
                    if current.recovery_node_run_id:
                        await heartbeat_session.execute(
                            update(NodeRun)
                            .where(NodeRun.id == current.recovery_node_run_id)
                            .values(
                                heartbeat_at=utcnow(),
                                lease_expires_at=current.lease_expires_at,
                            )
                        )
                    return False

            async with SessionLocal() as session:
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                kwargs: dict[str, Any] = {"host": self._host}
                if "cancellation" in inspect.signature(
                    run_recovery_agent
                ).parameters:
                    kwargs["cancellation"] = cancelled
                decision = await run_recovery_agent(
                    checkpoint, fallback_config, **kwargs
                )
        except asyncio.CancelledError:
            async with SessionLocal() as session, session.begin():
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                if checkpoint is not None:
                    checkpoint.status = "cancelled"
                    await self._finish_recovery_node(session, checkpoint, "cancelled")
            return True
        except RecoveryError as exc:
            async with SessionLocal() as session, session.begin():
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                run = await session.get(Run, checkpoint.run_id)
                checkpoint.status = "failed"
                await self._finish_recovery_node(session, checkpoint, "failed")
                if run is not None:
                    run.status = "failed"
                    run.finished_at = utcnow()
                    run.error_json = {"code": exc.code, "message": exc.message}
            return True

        # 3) 应用决定：只有 checkpoint 仍 running 且 run 仍 recovering 时才应用。
        async with SessionLocal() as session, session.begin():
            checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
            run = await session.get(Run, checkpoint.run_id)
            if checkpoint.status != "running" or run is None or run.status != "recovering":
                if checkpoint.status == "running":
                    checkpoint.status = "superseded"
                return True
            next_status = await apply_recovery_decision(session, checkpoint, decision)
            await self._finish_recovery_node(session, checkpoint, "succeeded")
            run_id = checkpoint.run_id
        if next_status == "running":
            async with SessionLocal() as session, session.begin():
                await Orchestrator().advance_run(session, run_id)
        return True

    async def _reclaim_expired(self, now: Any) -> None:
        """回收崩溃 Worker 遗留租约；未知结果必须再次人工确认。"""

        from app.infrastructure.database import SessionLocal

        async with SessionLocal() as session, session.begin():
            expired = (
                await session.scalars(
                    select(ExecutionCheckpoint).where(
                        ExecutionCheckpoint.status == "running",
                        ExecutionCheckpoint.lease_expires_at < now,
                    )
                )
            ).all()
            for checkpoint in expired:
                run = await session.get(Run, checkpoint.run_id)
                if checkpoint.recovery_node_run_id:
                    await session.execute(
                        update(NodeRun)
                        .where(
                            NodeRun.id == checkpoint.recovery_node_run_id,
                            NodeRun.status == "running",
                        )
                        .values(
                            status="lease_expired",
                            worker_id=None,
                            lease_expires_at=None,
                            finished_at=now,
                        )
                    )
                checkpoint.recovery_node_run_id = None
                checkpoint.lease_owner = None
                checkpoint.lease_expires_at = None
                certainty = (
                    checkpoint.snapshot_json.get("failedNode", {}).get(
                        "outcomeCertainty"
                    )
                )
                if certainty == "unknown":
                    checkpoint.status = "pending"
                    checkpoint.approval_status = "pending"
                    if run is not None:
                        run.status = "waiting_approval"
                    snapshot = {
                        "runId": checkpoint.run_id,
                        "nodeId": checkpoint.failed_node_id,
                        "title": "恢复结果未知，需要人工确认",
                        "description": "Recovery Worker 租约过期，无法确认外部结果。",
                        "payload": {"checkpointId": checkpoint.id},
                        "effect": "recovery.approval",
                        "checkpointId": checkpoint.id,
                    }
                    session.add(
                        Approval(
                            id=str(uuid4()),
                            run_id=checkpoint.run_id,
                            node_run_id=checkpoint.failed_node_run_id,
                            node_id=checkpoint.failed_node_id,
                            snapshot_digest=digest_json(snapshot),
                            snapshot_json=snapshot,
                            status="pending",
                        )
                    )
                else:
                    checkpoint.status = "pending"
                    if run is not None and run.status not in {
                        "cancelling",
                        "cancelled",
                    }:
                        run.status = "recovering"

    @staticmethod
    async def _finish_recovery_node(
        session: AsyncSession, checkpoint: ExecutionCheckpoint, status: str
    ) -> None:
        if not checkpoint.recovery_node_run_id:
            return
        await session.execute(
            update(NodeRun)
            .where(NodeRun.id == checkpoint.recovery_node_run_id)
            .values(
                status=status,
                worker_id=None,
                lease_expires_at=None,
                finished_at=utcnow(),
            )
        )
