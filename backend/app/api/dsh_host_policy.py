"""Consume an existing host permission grant without impersonating a human click.

This is provenance from the existing trusted loopback caller, not a signed remote
attestation. local.command inherits the same trust boundary as the host's shell.
"""

import hashlib
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dsh import bound_approval
from app.api.schemas import Decision
from app.application.compiler import digest_json
from app.application.run_authorization import approval_plan, operation_context
from app.infrastructure.database import get_session
from app.infrastructure.models.tasks import Invocation

router = APIRouter()
LOCAL_EFFECTS = frozenset(
    {"local.command", "none", "read", "data.read", "filesystem.read", "filesystem.write"}
)
STRUCTURAL_NODES = frozenset({"input", "output", "transform", "condition", "skillx"})


class HostPolicyGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_digest: str = Field(alias="snapshotDigest")
    plan_digest: str | None = Field(default=None, alias="planDigest")
    authorization_digest: str | None = Field(default=None, alias="authorizationDigest")
    host: Literal["dsh"]
    session_id: str = Field(alias="sessionId", min_length=1, max_length=200)
    call_id: str = Field(alias="callId", min_length=1, max_length=200)
    mode: Literal["danger-full-access"]
    workspace_root: str = Field(alias="workspaceRoot", min_length=1)
    approval_policy: Literal["never", "ask"] = Field(alias="approvalPolicy")
    policy_digest: str = Field(alias="policyDigest")

    def policy(self):
        return {
            "sessionId": self.session_id,
            "mode": self.mode,
            "workspaceRoot": self.workspace_root,
            "approvalPolicy": self.approval_policy,
        }


def validate_local_plan(plan):
    """Reject missing, dynamic, nested or explicitly external operation scopes."""
    if not isinstance(plan, dict) or not plan.get("operations"):
        raise HTTPException(
            403, {"code": "HOST_POLICY_SCOPE_UNSUPPORTED", "message": "缺少冻结本地操作计划"}
        )
    nodes = {node["id"]: node for node in plan["source"]["definition"]["spec"]["nodes"]}
    operations = {item["nodeId"]: item for item in plan["operations"]}
    for node_id in operations:
        node = nodes.get(node_id)
        if not node or node["type"] != "skillx" or node.get("fallback"):
            raise HTTPException(
                403, {"code": "HOST_POLICY_SCOPE_UNSUPPORTED", "message": "宿主权限不覆盖此操作"}
            )
    # Future operations outside this exact subset receive no grant, even if
    # their inputs become resolvable later. Their own snapshots are required.
    for operation in operations.values():
        lock = next(
            (
                lock
                for lock in plan["source"].get("validation", {}).get("capabilityLocks", [])
                if lock.get("nodeId") == operation["nodeId"]
            ),
            None,
        )
        target = operation.get("operation", {})
        if (
            not lock
            or lock.get("capabilityVersionId", lock.get("versionId")) != target.get("versionId")
            or lock.get("interface") != target.get("interface")
            or lock.get("digest") != operation.get("versionDigest")
        ):
            raise HTTPException(
                403, {"code": "HOST_POLICY_SCOPE_UNSUPPORTED", "message": "缺少匹配的能力版本锁"}
            )
        effects = operation.get("effects")
        if (
            not isinstance(effects, list)
            or not effects
            or any(not isinstance(e, dict) or e.get("type") not in LOCAL_EFFECTS for e in effects)
        ):
            raise HTTPException(
                403,
                {
                    "code": "HOST_POLICY_SCOPE_UNSUPPORTED",
                    "message": "宿主本地权限不覆盖未知或外部效果",
                },
            )


@router.post("/dsh/invocations/{invocation_id}/approvals/{approval_id}/host-policy")
async def grant_host_policy(
    invocation_id: str,
    approval_id: str,
    body: HostPolicyGrant,
    session: AsyncSession = Depends(get_session),
):
    from app.application.approval_transitions import approve

    decision = Decision(snapshotDigest=body.snapshot_digest)
    approval = await bound_approval(session, invocation_id, approval_id, decision)
    invocation = await session.get(Invocation, invocation_id)
    session_prefix = "dsh:" + hashlib.sha256(body.session_id.encode()).hexdigest() + ":"
    if not invocation.call_key.startswith(session_prefix):
        raise HTTPException(409, "宿主权限会话与调用不匹配")
    if (
        not Path(body.workspace_root).is_absolute()
        or digest_json(body.policy()) != body.policy_digest
    ):
        raise HTTPException(409, "宿主权限证据不完整或摘要变化")
    effect = approval.snapshot_json.get("effect")
    if effect == "workflow.run":
        frozen = await approval_plan(session, approval.run_id)
        snapshot_context = approval.snapshot_json.get("plan")
        if not frozen or frozen != snapshot_context:
            raise HTTPException(409, "冻结工作流计划已变化")
        validate_local_plan(frozen)
        expected_digest = digest_json(frozen)
    elif effect == "skillx.invoke":
        frozen = await operation_context(
            session,
            approval.run_id,
            approval.node_run_id,
            approval.snapshot_json.get("operation", {}),
        )
        if not frozen or frozen != approval.snapshot_json.get("operationContext"):
            raise HTTPException(409, "当前操作输入或上游执行证据已变化")
        if frozen.get("kind") != "direct" or frozen.get("capabilities"):
            raise HTTPException(
                403,
                {
                    "code": "HOST_POLICY_SCOPE_UNSUPPORTED",
                    "message": "宿主权限不覆盖模型或嵌套能力操作",
                },
            )
        effects = frozen.get("effects", [])
        if not effects or any(e.get("type") not in LOCAL_EFFECTS for e in effects):
            raise HTTPException(
                403,
                {
                    "code": "HOST_POLICY_SCOPE_UNSUPPORTED",
                    "message": "宿主权限不覆盖未知或外部效果",
                },
            )
        expected_digest = digest_json(frozen)
    else:
        raise HTTPException(
            403, {"code": "HOST_POLICY_SCOPE_UNSUPPORTED", "message": "宿主权限不覆盖此审批类型"}
        )
    supplied = [
        value for value in (body.plan_digest, body.authorization_digest) if value is not None
    ]
    if not supplied or any(value != expected_digest for value in supplied):
        raise HTTPException(409, "授权摘要缺失、不匹配或冲突")
    provenance = {
        "source": "host-policy",
        "host": body.host,
        "invocationId": invocation.id,
        "callId": body.call_id,
        "policy": body.policy(),
        "policyDigest": body.policy_digest,
        "snapshotDigest": body.snapshot_digest,
        "authorizationDigest": expected_digest,
        **({"planDigest": expected_digest} if effect == "workflow.run" else {}),
        "effect": effect,
        "scope": "host-local-process-permission",
    }
    result = await approve(approval.id, decision, session, provenance=provenance)
    await session.commit()
    return result
