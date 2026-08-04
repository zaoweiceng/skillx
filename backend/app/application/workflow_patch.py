from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 005-E：失败可复现性与受控 WorkflowPatch。
#
# 一次瞬态波动不提版本修改；同版本、同 failureFingerprint 可重复复现且修复回放成功时，
# 才允许生成 WorkflowPatch 草稿。通用 WorkflowPatch 只表达节点/边/配置/契约的受限变更，
# 不能携带宿主机路径、Secret 或扩大能力权限。应用补丁总是创建 draft，人工发布后才生效。

FailureClass = Literal[
    "transient", "contract_drift", "deterministic_input", "platform_bug", "unknown"
]

# 补丁绝不允许出现的敏感/越权字段。
_FORBIDDEN_CONFIG_KEYS = frozenset(
    {"package", "packagePath", "command", "secretRef", "secret", "apiKey", "token"}
)
# 只允许补丁修改的节点字段（受限变更集）。
_ALLOWED_NODE_FIELDS = frozenset(
    {"config", "input", "timeoutSeconds", "retry", "fallback", "displayName"}
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
    """递归拒绝宿主机路径/Secret 等敏感键。"""

    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_CONFIG_KEYS:
                raise WorkflowPatchViolation(f"补丁不得携带敏感字段 {path}{key}")
            _reject_forbidden(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item, path)


def apply_workflow_patch(
    definition: dict[str, Any],
    patch: WorkflowPatch,
) -> dict[str, Any]:
    """把受控 WorkflowPatch 应用到草稿 definition，返回新的 definition。

    安全边界：只改允许的节点字段；禁止修改 type/sideEffect/idempotent/permissions；
    禁止携带宿主机路径/Secret；不得新增未在原图中的能力权限。应用产生 draft，人工发布
    后才生效。
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
            # config 做浅合并，其余字段整体替换。
            merge_config = (
                field == "config"
                and isinstance(value, dict)
                and isinstance(node.get("config"), dict)
            )
            if merge_config:
                node["config"] = {**node["config"], **value}
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
