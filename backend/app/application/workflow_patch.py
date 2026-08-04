from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import FailureObservation, utcnow

# 006-D/005-E：失败可复现性与受控 WorkflowPatch。
#
# 一次瞬态波动不提版本修改；同版本、同 failureFingerprint 可重复复现且修复回放成功时，
# 才允许生成 WorkflowPatch 草稿。资格由后端事实（持久 FailureObservation 的 occurrence +
# classification）计算，不接受前端自报 reproducible。通用 WorkflowPatch 只表达节点/边/配置/
# 契约的受限变更，不能携带宿主机路径、Secret 或扩大能力/锁/模型/权限。应用补丁总是创建
# draft，真实 replay 通过后才可发布。

FailureClass = Literal[
    "transient", "contract_drift", "deterministic_input", "platform_bug", "unknown"
]

# 出现次数只用于聚合热度，不作为 baseline reproduction 证据。
REPRODUCIBLE_THRESHOLD = 2

# 补丁绝不允许出现的敏感/越权字段（键名规范化后递归匹配，防大小写/嵌套绕过）。
_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "package",
        "packagepath",
        "command",
        "secretref",
        "secret",
        "apikey",
        "token",
        # 能力锁 / 权限 / 模型 / 副作用 / 幂等 / 风险：不得通过普通 Patch 修改。
        "allowedcapabilities",
        "mcptoolsnapshotid",
        "capabilityversionid",
        "capabilityref",
        "model",
        "modelprofile",
        "permissions",
        "sideeffect",
        "idempotent",
        "risk",
        "risklevel",
    }
)
# 只允许补丁修改的节点字段（受限变更集）。config 走字段级白名单，不做任意浅合并。
_ALLOWED_NODE_FIELDS = frozenset(
    {"config", "input", "timeoutSeconds", "retry", "displayName"}
)
# config 内允许调整的键（不含任何能力/锁/权限/模型）。
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "operation",
        "table",
        "keyField",
        "default",
        "template",
        "fields",
        "mapping",
        "maxSteps",
        "maxTotalTokens",
        "maxOutputTokens",
        "prompt",
        "outputSchema",
        "navigation",
        "timeoutMs",
        "extract",
        "successAssertions",
        "resultSelectors",
    }
)


class WorkflowPatchViolation(ValueError):
    """WorkflowPatch 试图携带宿主机路径、Secret 或扩大能力/权限。"""


def failure_fingerprint(
    *,
    workflow_version_id: str,
    node_id: str,
    node_type: str,
    error_code: str,
    capability_lock: str | None = None,
    contract_summary: dict[str, Any] | None = None,
) -> str:
    """稳定失败指纹：只覆盖版本、节点 id/type、能力锁、错误码与稳定契约/环境摘要。

    显式排除时间戳、随机 run/node id、原始 Secret 等不稳定内容，使“同一可复现故障”
    在多次运行间得到相同指纹。
    """

    payload = {
        "workflowVersionId": workflow_version_id,
        "nodeId": node_id,
        "nodeType": node_type,
        "errorCode": error_code,
        "capabilityLock": capability_lock,
        "contractSummary": contract_summary or {},
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def classify_failure(error_code: str, *, recovered: bool, reproducible: bool) -> FailureClass:
    """把失败归类，决定是否值得生成 WorkflowPatch。

    - 网络/依赖类且未稳定复现 → transient；
    - 契约漂移错误码 → contract_drift；
    - 可复现的输入/校验类 → deterministic_input；
    - 其它可复现 → platform_bug；否则 unknown。
    """

    if error_code in {"DEPENDENCY_UNAVAILABLE", "TIMEOUT"} and not reproducible:
        return "transient"
    if error_code in {"MCP_CONTRACT_DRIFT", "PAGE_DRIFT"}:
        return "contract_drift"
    if not reproducible:
        return "transient" if not recovered else "unknown"
    if error_code in {"INVALID_INPUT", "CONTRACT_VIOLATION"}:
        return "deterministic_input"
    return "platform_bug"


def should_propose_patch(failure_class: FailureClass, *, reproducible: bool) -> bool:
    """只有可复现的非瞬态故障才允许生成 WorkflowPatch 草稿。"""

    return reproducible and failure_class in {
        "contract_drift",
        "deterministic_input",
        "platform_bug",
    }


class NodePatch(BaseModel):
    node_id: str = Field(alias="nodeId")
    # 只允许受限字段的浅覆盖；禁止改 type/id/sideEffect/idempotent/permissions。
    changes: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WorkflowPatch(BaseModel):
    """通用工作流补丁：只表达节点/边/配置/契约的受限变更。"""

    kind: Literal["WorkflowPatch"] = "WorkflowPatch"
    base_version_id: str = Field(alias="baseVersionId")
    node_patches: list[NodePatch] = Field(default_factory=list, alias="nodePatches")
    add_edges: list[dict[str, str]] = Field(default_factory=list, alias="addEdges")
    remove_edges: list[dict[str, str]] = Field(default_factory=list, alias="removeEdges")
    reason: str = ""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


def _reject_forbidden(value: Any, path: str = "") -> None:
    """递归拒绝宿主机路径/Secret/能力锁等敏感键；键名规范化（小写去分隔符）后匹配，
    避免大小写/嵌套绕过。"""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if normalized in _FORBIDDEN_CONFIG_KEYS:
                raise WorkflowPatchViolation(f"补丁不得携带敏感/能力字段 {path}{key}")
            _reject_forbidden(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item, path)


def apply_workflow_patch(
    definition: dict[str, Any],
    patch: WorkflowPatch,
) -> dict[str, Any]:
    """把受控 WorkflowPatch 应用到草稿 definition，返回新的 definition。

    安全边界：只改允许的节点字段；config 只允许字段级白名单键（不含能力锁/权限/模型/
    副作用/幂等）；禁止修改 type/sideEffect/idempotent/permissions/fallback；禁止携带
    宿主机路径/Secret；不得新增/替换能力锁。边变更后须重做 reachability/数据依赖检查
    （由调用方 Compiler 回放完成）。应用产生 draft，真实 replay 通过后才可发布。
    """

    next_definition = deepcopy(definition)
    nodes = {node["id"]: node for node in next_definition.get("spec", {}).get("nodes", [])}
    for node_patch in patch.node_patches:
        node = nodes.get(node_patch.node_id)
        if node is None:
            raise WorkflowPatchViolation(f"补丁目标节点 {node_patch.node_id} 不存在")
        for field, value in node_patch.changes.items():
            if field not in _ALLOWED_NODE_FIELDS:
                raise WorkflowPatchViolation(f"补丁不得修改节点字段 {field}")
            _reject_forbidden({field: value})
            if field == "config" and isinstance(value, dict):
                # config 字段级白名单合并：只允许安全键，不做任意浅合并。
                for config_key in value:
                    if config_key not in _ALLOWED_CONFIG_KEYS:
                        raise WorkflowPatchViolation(
                            f"补丁不得修改 config 键 {config_key}（不在受限白名单内）"
                        )
                base = node.get("config") if isinstance(node.get("config"), dict) else {}
                node["config"] = {**base, **value}
            else:
                node[field] = value
    edges = next_definition.setdefault("spec", {}).setdefault("edges", [])
    for edge in patch.remove_edges:
        edges[:] = [
            item
            for item in edges
            if not (
                item.get("source") == edge.get("source")
                and item.get("target") == edge.get("target")
            )
        ]
    for edge in patch.add_edges:
        if edge.get("source") not in nodes or edge.get("target") not in nodes:
            raise WorkflowPatchViolation("补丁新增边的端点必须是已有节点")
        edges.append({"source": edge["source"], "target": edge["target"]})
    return next_definition


async def record_failure_observation(
    session: AsyncSession,
    *,
    workflow_version_id: str,
    node_id: str,
    node_type: str,
    error_code: str,
    contract_summary: dict[str, Any] | None = None,
    recovered: bool = False,
    run_id: str | None = None,
    checkpoint_id: str | None = None,
) -> FailureObservation:
    """按稳定 fingerprint 聚合失败观测；重复失败累加 occurrence（后端事实来源）。"""

    fingerprint = failure_fingerprint(
        workflow_version_id=workflow_version_id,
        node_id=node_id,
        node_type=node_type,
        error_code=error_code,
        contract_summary=contract_summary,
    )
    classification = classify_failure(
        error_code, recovered=recovered, reproducible=False
    )
    now = utcnow()
    statement = sqlite_insert(FailureObservation).values(
        id=str(uuid4()),
        fingerprint=fingerprint,
        workflow_version_id=workflow_version_id,
        node_id=node_id,
        node_type=node_type,
        error_code=error_code,
        classification=classification,
        occurrence=1,
        recovered=recovered,
        last_run_id=run_id,
        last_checkpoint_id=checkpoint_id,
        contract_summary_json=contract_summary or {},
        created_at=now,
        updated_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[FailureObservation.fingerprint],
            set_={
                "occurrence": FailureObservation.occurrence + 1,
                "recovered": FailureObservation.recovered | recovered,
                "last_run_id": run_id,
                "last_checkpoint_id": checkpoint_id,
                "updated_at": now,
            },
        )
    )
    observation = await session.scalar(
        select(FailureObservation).where(FailureObservation.fingerprint == fingerprint)
    )
    assert observation is not None
    reproducible = bool(observation.baseline_reproduced)
    observation.classification = classify_failure(
        error_code, recovered=observation.recovered, reproducible=reproducible
    )
    await session.flush()
    return observation


def observation_eligibility(observation: FailureObservation) -> dict[str, Any]:
    """由后端事实计算 Patch 资格：只有可复现（occurrence≥阈值）的非瞬态故障才 eligible。

    返回 {eligible, reason, reproducible, occurrence, classification}，前端不能自报。
    """

    reproducible = bool(observation.baseline_reproduced)
    classification = classify_failure(
        observation.error_code, recovered=observation.recovered, reproducible=reproducible
    )
    eligible = should_propose_patch(classification, reproducible=reproducible)
    if not reproducible:
        reason = "尚未通过隔离 baseline reproduction"
    elif not eligible:
        reason = f"分类 {classification} 不适合生成补丁（瞬态或未知）"
    else:
        reason = "可复现的非瞬态故障，允许生成受控补丁 proposal"
    return {
        "eligible": eligible,
        "reason": reason,
        "reproducible": reproducible,
        "occurrence": observation.occurrence,
        "classification": classification,
    }


async def replay_patched_definition(
    patched_definition: dict[str, Any],
    *,
    safe_inputs: dict[str, Any],
    fail_node_id: str,
) -> dict[str, Any]:
    """在隔离受限环境用安全输入真实回放 patched 图，验证原失败点不再复现。

    这不是静态编译校验：它用内存执行器实际跑到失败节点及必要下游，比较结果。返回
    {replayRunId, passed, failedNodeStatus, outputs, diagnostics}。副作用节点在 replay
    中以幂等/模拟语义执行；替代输入取自 source checkpoint 的安全值。
    """

    from app.application.replay import run_isolated_replay

    return await run_isolated_replay(
        patched_definition, safe_inputs=safe_inputs, fail_node_id=fail_node_id
    )
