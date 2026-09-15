from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .runs import Artifact


class ExecutionCheckpoint(Base, TimestampMixin):
    """节点重试耗尽且命中 fallback 时冻结的恢复检查点。

    保存已完成输出/Artifact 引用、失败输入/标准错误、capability locks、剩余子图与
    已提交副作用；恢复只消费该持久化 checkpoint，不依赖原 Worker 内存。
    """

    __tablename__ = "execution_checkpoints"
    __table_args__ = (
        Index("ix_execution_checkpoints_run_id", "run_id"),
        Index(
            "uq_checkpoint_failed_attempt",
            "failed_node_run_id",
            "attempt",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    workflow_version_id: Mapped[str] = mapped_column(String(36))
    failed_node_id: Mapped[str] = mapped_column(String(64))
    failed_node_run_id: Mapped[str] = mapped_column(String(36))
    attempt: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # 原子领取租约：单条 CAS 把 pending→running 并写入 owner/expiry/heartbeat，未领取成功
    # 的 Worker 不得调用 Agent。
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # 该次恢复实际执行的 Recovery NodeRun（唯一）。
    recovery_node_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 审批型 fallback：none|pending|approved|rejected。
    approval_status: Mapped[str] = mapped_column(String(20), default="none")


class OutputSubstitution(Base, TimestampMixin):
    """恢复提供的替代输出（不覆盖失败 NodeRun）。

    原失败 attempt 永远不可变；恢复从 checkpoint 剩余子图续跑时，下游节点显式从这里读取
    被替代节点的输出。每个 (run_id, node_id) 至多一条替代。
    """

    __tablename__ = "output_substitutions"
    __table_args__ = (Index("uq_output_substitution_node", "run_id", "node_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    checkpoint_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(64))
    failed_node_run_id: Mapped[str] = mapped_column(String(36))
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(40), default="recovery")


class FailureObservation(Base, TimestampMixin):
    """按稳定 fingerprint 聚合的失败观测（受控 WorkflowPatch 的资格依据）。

    瞬态错误默认只记录并累加 occurrence；只有可复现的非瞬态 fingerprint 才允许生成 Patch
    proposal。资格由后端事实（occurrence + classification）计算，不接受前端自报。
    """

    __tablename__ = "failure_observations"
    __table_args__ = (
        CheckConstraint(
            "(workflow_version_id IS NOT NULL) != (plan_snapshot_id IS NOT NULL)",
            name="ck_failure_observation_source",
        ),
        Index("uq_failure_observation_fingerprint", "fingerprint", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(80))
    workflow_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_node_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    node_id: Mapped[str] = mapped_column(String(64))
    node_type: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(32))
    occurrence: Mapped[int] = mapped_column(Integer, default=1)
    recovered: Mapped[bool] = mapped_column(default=False)
    baseline_reproduced: Mapped[bool] = mapped_column(default=False)
    evidence_kind: Mapped[str] = mapped_column(String(40), default="unsupported")
    reproduction_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    locks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    contract_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recovered_outcome_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class TaskCheckpoint(Base, TimestampMixin):
    __tablename__ = "task_checkpoints"
    __table_args__ = (Index("ix_task_checkpoint_source", "source_kind", "source_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str] = mapped_column(String(36))
    source_digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(80))
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_generation: Mapped[int] = mapped_column(Integer, default=0)
    claim_token: Mapped[str | None] = mapped_column(String(80), nullable=True)


class RecoveryAttempt(Base, TimestampMixin):
    __tablename__ = "recovery_attempts"
    __table_args__ = (Index("uq_recovery_attempt", "checkpoint_id", "attempt", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("execution_checkpoints.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(Integer)
    plan_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))


class ReplayEvidence(Base, TimestampMixin):
    __tablename__ = "replay_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("failure_observations.id", ondelete="CASCADE"), index=True
    )
    baseline_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    patched_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(default=False)
    fixture_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    base_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    patch_of_observation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    base_definition_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    patch_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    patched_definition_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    human_patch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class RevisionProposal(Base, TimestampMixin):
    __tablename__ = "revision_proposals"
    __table_args__ = (
        Index(
            "uq_revision_proposal_content",
            "observation_id",
            "baseline_digest",
            "proposal_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("failure_observations.id", ondelete="CASCADE"), index=True
    )
    base_version_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_versions.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(40))
    fixture_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    baseline_digest: Mapped[str] = mapped_column(String(80))
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    proposal_digest: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="proposed")


class HumanPatch(Base, TimestampMixin):
    __tablename__ = "human_patches"
    __table_args__ = (
        Index(
            "uq_human_patch_content",
            "parent_proposal_id",
            "patch_digest",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_proposal_id: Mapped[str] = mapped_column(
        ForeignKey("revision_proposals.id", ondelete="CASCADE"), index=True
    )
    editor_id: Mapped[str] = mapped_column(String(120), default="local-user")
    patch_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    patch_digest: Mapped[str] = mapped_column(String(80))


class ReplayFixture(Base, TimestampMixin):
    __tablename__ = "replay_fixtures"
    __table_args__ = (Index("uq_replay_fixture_node_run", "node_run_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    node_run_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(64))
    artifact: Mapped[Artifact] = relationship()
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"))
    source_digest: Mapped[str] = mapped_column(String(80))
    input_digest: Mapped[str] = mapped_column(String(80))
    output_digest: Mapped[str] = mapped_column(String(80))
    capture_policy: Mapped[str] = mapped_column(String(40))
    evidence_kind: Mapped[str] = mapped_column(String(40), default="unsupported")
