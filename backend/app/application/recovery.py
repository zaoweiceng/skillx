from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent_host import AgentHost, AgentHostError, AgentHostRequest
from app.application.compiler import digest_json
from app.application.security import redact
from app.config import get_settings
from app.domain.workflow import NON_RECOVERABLE_ERROR_CODES, NodeType, WorkflowDefinition
from app.infrastructure.database import (
    ExecutionCheckpoint,
    NodeRun,
    OutputSubstitution,
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
        "reason": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": True,
}


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
    substitutions = await load_substitutions(session, run.id)
    completed_outputs: dict[str, Any] = {}
    committed_side_effects: list[dict[str, Any]] = []
    for node_id, node_run in latest.items():
        if node_run.status == "succeeded":
            completed_outputs[node_id] = node_run.output_json
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
    snapshot = {
        "workflowVersionId": run.workflow_version_id,
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
        "capabilityLocks": _capability_locks(run),
        "fallbackConfig": fallback_config,
        "runInput": run.input_json,
    }
    approval_status = "pending" if fallback_config.get("requireUserApproval") else "none"
    checkpoint = ExecutionCheckpoint(
        id=str(uuid4()),
        run_id=run.id,
        workflow_version_id=run.workflow_version_id,
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


def _capability_locks(run: Run) -> list[dict[str, Any]]:
    """从 Run 关联的锁摘要中提取能力锁（若已附加），否则空列表。"""

    locks = getattr(run, "_capability_locks", None)
    return list(locks) if isinstance(locks, list) else []


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
        "outputContract": snapshot.get("outputContract"),
    }
    return (
        "你是受限恢复 Agent。已发布工作流的某节点重试耗尽，请基于以下 checkpoint 上下文"
        "决定如何恢复。只能返回 RecoveryDecision，action ∈ "
        "[provide_substitution, retry_replanned, request_user, abort]。"
        "provide_substitution 时必须给出 substitution 对象作为失败节点的替代输出，"
        "并应复用 completedOutputs 中的已完成值；已完成的非幂等副作用不得重放；"
        "outcomeCertainty 为 unknown 时选择 request_user。\n"
        f"context: {json.dumps(context, ensure_ascii=False)}"
    )


def parse_recovery_decision(raw: dict[str, Any]) -> dict[str, Any]:
    """把 Agent 原始输出解析为规范化 RecoveryDecision，非法时 fail closed。

    - action 必须在允许集合内；
    - provide_substitution 必须带 substitution 对象，其它 action 不得带 substitution。
    """

    action = raw.get("action")
    if action not in RECOVERY_ACTIONS:
        raise RecoveryError("CONTRACT_VIOLATION", f"非法 RecoveryDecision action: {action}")
    substitution = raw.get("substitution")
    if action == "provide_substitution":
        if not isinstance(substitution, dict):
            raise RecoveryError(
                "CONTRACT_VIOLATION", "provide_substitution 需要 substitution 对象"
            )
    elif substitution is not None:
        raise RecoveryError(
            "CONTRACT_VIOLATION", f"{action} 不得携带 substitution 字段"
        )
    return {"action": action, "substitution": substitution, "reason": raw.get("reason", "")}


async def run_recovery_agent(
    checkpoint: ExecutionCheckpoint,
    fallback_config: dict[str, Any],
    *,
    host: AgentHost | None = None,
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
        result = await host.execute(request)
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
        run.status = "waiting_approval"
        checkpoint.status = "awaiting_user"
        await session.flush()
        return "waiting_approval"
    if action == "provide_substitution":
        output = decision["substitution"]
        _validate_substitution(checkpoint, output)
        await _record_substitution(session, checkpoint, output, source="recovery")
    else:  # retry_replanned
        await _record_substitution(session, checkpoint, {}, source="skip")
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
    }


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
                inserted = await session.execute(
                    sqlite_insert(NodeRun)
                    .values(
                        id=recovery_node_run_id,
                        run_id=candidate.run_id,
                        node_id=target,
                        node_type="agent",
                        attempt=1,
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
            async with SessionLocal() as session:
                checkpoint = await session.get(ExecutionCheckpoint, checkpoint_id)
                decision = await run_recovery_agent(
                    checkpoint, fallback_config, host=self._host
                )
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
