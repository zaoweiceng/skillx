from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.database import Component, WorkflowVersion

# 候选消息状态机：component_candidate → collecting_inputs → confirmed → component_run。
CANDIDATE_STATES = (
    "component_candidate",
    "collecting_inputs",
    "confirmed",
    "component_run",
)


@dataclass
class ComponentCandidate:
    component_id: str
    component_name: str
    workflow_version_id: str
    input_schema: dict[str, Any]
    ui_schema: dict[str, Any]
    execution_class: str
    confidence: float
    reason: str


def candidate_snapshot_digest(
    component_id: str,
    workflow_version_id: str,
    input_schema: dict[str, Any],
) -> str:
    """候选组件版本与输入契约的快照摘要。

    这不是密钥令牌，也不保证一次性：它是对公开的 component/version/inputSchema 的
    确定性 digest。确认时服务端重新计算并比对，用于检测“候选展示后默认版本或输入
    Schema 已变化”（stale），而不是授权。
    """

    payload = {
        "componentId": component_id,
        "workflowVersionId": workflow_version_id,
        "inputSchema": input_schema,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _score(component: Component, text: str) -> tuple[float, str]:
    """结构化意图打分：不再以“组件名是否为输入子串”作为唯一依据。

    综合名称词元、描述词元与显式动词/意图词的命中比例，返回 [0,1] 置信度与原因。
    无模型配置时这是可解释的降级匹配依据，由前端呈现为手动候选。
    """

    haystack = text.lower()
    name_tokens = [token for token in component.name.lower().split() if token]
    if not name_tokens:
        name_tokens = [component.name.lower()]
    desc_tokens = [token for token in (component.description or "").lower().split() if token]

    name_hits = sum(1 for token in name_tokens if token in haystack)
    desc_hits = sum(1 for token in desc_tokens if len(token) > 1 and token in haystack)

    reasons: list[str] = []
    score = 0.0
    if name_hits:
        ratio = name_hits / len(name_tokens)
        score += 0.6 * ratio
        reasons.append(f"名称词元命中 {name_hits}/{len(name_tokens)}")
    if desc_hits:
        # 描述命中即给予实质权重：每命中一个显著词元贡献 0.35，上限 0.7。
        score += min(0.7, 0.35 * desc_hits)
        reasons.append(f"描述词元命中 {desc_hits}")
    # 完整名称直接出现给予加成，但不再是唯一路径。
    if component.name.lower() in haystack:
        score = max(score, 0.75)
        reasons.append("包含组件全名")
    return min(1.0, score), "；".join(reasons) or "无显著匹配"


async def match_component(
    session: AsyncSession,
    text: str,
    *,
    threshold: float = 0.3,
) -> ComponentCandidate | None:
    """从组件目录中按结构化意图匹配最佳候选。"""

    components = (await session.scalars(select(Component))).all()
    best: tuple[float, str, Component] | None = None
    for component in components:
        score, reason = _score(component, text)
        if best is None or score > best[0]:
            best = (score, reason, component)
    if best is None or best[0] < threshold:
        return None
    score, reason, component = best
    version = await session.get(WorkflowVersion, component.default_version_id)
    if version is None:
        return None
    spec = version.definition_json["spec"]
    return ComponentCandidate(
        component_id=component.id,
        component_name=component.name,
        workflow_version_id=version.id,
        input_schema=spec["inputSchema"],
        ui_schema=spec.get("uiSchema", {}),
        execution_class=component.execution_class,
        confidence=round(score, 3),
        reason=reason,
    )


def candidate_message(
    candidate: ComponentCandidate,
    *,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    """构造锁定版本、inputSchema 和候选令牌的候选消息内容。"""

    return {
        "type": "component_candidate",
        "state": "component_candidate",
        "componentId": candidate.component_id,
        "componentName": candidate.component_name,
        "workflowVersionId": candidate.workflow_version_id,
        "inputSchema": candidate.input_schema,
        "uiSchema": candidate.ui_schema,
        "executionClass": candidate.execution_class,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
        "candidateSnapshotDigest": candidate_snapshot_digest(
            candidate.component_id,
            candidate.workflow_version_id,
            candidate.input_schema,
        ),
        "required": missing if missing is not None else candidate.input_schema.get("required", []),
        "text": "发现匹配组件，请确认并补充参数后启动，或继续通用 Agent。",
    }
