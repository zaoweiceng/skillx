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

    model_config = ConfigDict(populate_by_name=True)


class WorkflowNode(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
    type: NodeType
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=30, alias="timeoutSeconds", ge=1, le=86400)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    on_error: Literal["fail_run", "continue"] = Field(default="fail_run", alias="onError")
    permissions: list[str] = Field(default_factory=list)
    side_effect: bool = Field(default=False, alias="sideEffect")
    idempotent: bool = True

    model_config = ConfigDict(populate_by_name=True)


class WorkflowEdge(BaseModel):
    source: str
    target: str
    when: bool | None = None


class WorkflowPolicies(BaseModel):
    max_duration_seconds: int = Field(default=300, alias="maxDurationSeconds", ge=1)
    max_parallel_nodes: int = Field(default=4, alias="maxParallelNodes", ge=1, le=64)

    model_config = ConfigDict(populate_by_name=True)


class WorkflowSpec(BaseModel):
    input_schema: dict[str, Any] = Field(alias="inputSchema")
    ui_schema: dict[str, Any] = Field(default_factory=dict, alias="uiSchema")
    output_schema: dict[str, Any] = Field(alias="outputSchema")
    nodes: list[WorkflowNode] = Field(min_length=2)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    policies: WorkflowPolicies = Field(default_factory=WorkflowPolicies)

    model_config = ConfigDict(populate_by_name=True)

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

    model_config = ConfigDict(populate_by_name=True)


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
    status: Literal["succeeded", "waiting_user", "failed"]
    output: dict[str, Any] | None = None
    error: ExecutionError | None = None
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    resume_token: str | None = None
