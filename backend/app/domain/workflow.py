from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeType(StrEnum):
    INPUT = "input"
    TRANSFORM = "transform"
    CONDITION = "condition"
    OUTPUT = "output"
    APPROVAL = "approval"
    SKILLX = "skillx"
    MCP = "mcp"
    LLM = "llm"
    AGENT = "agent"
    BROWSER = "browser"
    ARTIFACT = "artifact"


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, alias="maxAttempts", ge=1, le=10)
    backoff_seconds: float = Field(default=1, alias="backoffSeconds", ge=0, le=3600)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


# 默认不允许自动交给 Agent 恢复的 error code（安全/契约/结果未知类）。
# 只有节点显式声明且带审批策略时才可进入恢复；见 compiler 的 fallback 校验。
NON_RECOVERABLE_ERROR_CODES = frozenset(
    {"PERMISSION_DENIED", "AUTHENTICATION_FAILED", "CONTRACT_VIOLATION", "UNKNOWN_OUTCOME"}
)


class FallbackPolicy(BaseModel):
    """节点级显式 fallback：某些错误码触发时把 checkpoint 交给受限 Agent 恢复。

    fallback 必须写进已发布版本；只处理声明的错误码，Direct/MCP/Browser 失败不得隐式
    升级为 Agent。``requireUserApproval`` 为 True 时恢复前需人工确认。
    """

    on: list[str] = Field(min_length=1)
    target: str
    require_user_approval: bool = Field(default=False, alias="requireUserApproval")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WorkflowNode(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    type: NodeType
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, alias="timeoutSeconds", ge=1, le=86400)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    on_error: Literal["fail_run", "continue"] = Field(default="fail_run", alias="onError")
    fallback: FallbackPolicy | None = None
    permissions: list[str] = Field(default_factory=list)
    side_effect: bool = Field(default=False, alias="sideEffect")
    idempotent: bool = True

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class WorkflowEdge(BaseModel):
    source: str
    target: str
    when: bool | None = None

    model_config = ConfigDict(extra="forbid")


class WorkflowPolicies(BaseModel):
    max_duration_seconds: int = Field(default=300, alias="maxDurationSeconds", ge=1)
    max_parallel_nodes: int = Field(default=4, alias="maxParallelNodes", ge=1, le=64)

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ModelRuntimePolicy(BaseModel):
    order: list[Literal["llm_runtime", "agent_runtime"]] = Field(
        default_factory=lambda: ["llm_runtime", "agent_runtime"]
    )
    fallback: Literal["startup_unavailable_only"] = "startup_unavailable_only"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def fixed_order(self) -> ModelRuntimePolicy:
        if self.order != ["llm_runtime", "agent_runtime"]:
            raise ValueError("modelRuntimePolicy.order must be llm_runtime then agent_runtime")
        return self


class WorkflowSpec(BaseModel):
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    ui_schema: dict[str, Any] = Field(default_factory=dict, alias="uiSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    nodes: list[WorkflowNode] = Field(min_length=2)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    policies: WorkflowPolicies = Field(default_factory=WorkflowPolicies)
    model_runtime_policy: ModelRuntimePolicy = Field(
        default_factory=ModelRuntimePolicy, alias="modelRuntimePolicy"
    )

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    @model_validator(mode="after")
    def unique_node_ids(self) -> WorkflowSpec:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("node id must be unique")
        return self


class WorkflowDefinition(BaseModel):
    api_version: Literal["workflow.platform/v1"] = Field(alias="apiVersion")
    kind: Literal["Workflow"] = "Workflow"
    metadata: dict[str, Any]
    spec: WorkflowSpec

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class Diagnostic(BaseModel):
    level: Literal["error", "warning"]
    code: str
    path: str
    message: str


class CompiledWorkflow(BaseModel):
    definition: WorkflowDefinition
    topological_order: list[str]
    dependencies: dict[str, list[str]]
    diagnostics: list[Diagnostic]
    digest: str


class ExecutionError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class NodeResult(BaseModel):
    status: Literal["succeeded", "waiting_user", "failed", "cancelled"]
    output: dict[str, Any] | None = None
    error: ExecutionError | None = None
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
