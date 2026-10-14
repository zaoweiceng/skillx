from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]


class DraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    definition: dict[str, Any]


class PublishRequest(BaseModel):
    draft_revision: int = Field(alias="draftRevision", ge=1)
    draft_digest: str = Field(alias="draftDigest", min_length=8, max_length=80)
    replay_evidence_id: str | None = Field(
        default=None, alias="replayEvidenceId"
    )


class ComponentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    workflow_version_id: str = Field(alias="workflowVersionId")
    visibility: str = "private"


class RunCreate(BaseModel):
    input: dict[str, Any]


class Decision(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")


class RecoveryAnswer(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")
    answer: Any


class TaskCheckpointAnswer(BaseModel):
    snapshot_digest: str = Field(alias="snapshotDigest")
    answer: dict[str, Any]


class CapabilityCreate(BaseModel):
    name: str
    type: str
    package_path: str | None = Field(default=None, alias="packagePath")
    interface: str | None = None


class CapabilityExecute(BaseModel):
    interface: str = Field(min_length=1)
    input: dict[str, Any]


class MCPServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: str
    config: dict[str, Any]


class MCPToolTest(BaseModel):
    arguments: dict[str, Any]


class MCPToolClassify(BaseModel):
    effect: str
    idempotent: bool


class SecretReferenceCreate(BaseModel):
    name: str
    environment_variable: str = Field(alias="environmentVariable")
    description: str = ""


class ConversationCreate(BaseModel):
    workspace_path: str | None = Field(default=None, alias="workspacePath", max_length=4096)
    title: str = "新对话"
    bound_component_id: str | None = Field(default=None, alias="boundComponentId")


class ConversationModeUpdate(BaseModel):
    workflow_mode: str = Field(alias="workflowMode", pattern="^(enabled|disabled)$")


class ChatReferenceFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=128 * 1024)


class ChatImage(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mime_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"] = Field(
        alias="mimeType"
    )
    data: str = Field(min_length=1, max_length=7 * 1024 * 1024)


class MessageCreate(BaseModel):
    working_directory: str | None = Field(default=None, alias="workingDirectory", max_length=4096)
    content: str
    component_id: str | None = Field(default=None, alias="componentId")
    inputs: dict[str, Any] | None = None
    images: list[ChatImage] = Field(default_factory=list, max_length=8)
    reference_files: list[ChatReferenceFile] = Field(
        default_factory=list, alias="referenceFiles", max_length=8
    )

    @model_validator(mode="after")
    def validate_reference_files(self):
        from app.application.chat_images import MAX_TOTAL_IMAGE_BYTES, decode_image

        image_bytes = sum(len(decode_image(image.data, image.mime_type)) for image in self.images)
        if image_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("图片总大小不能超过 20 MB")
        sizes = [len(item.content.encode("utf-8")) for item in self.reference_files]
        if any(size > 128 * 1024 for size in sizes) or sum(sizes) > 256 * 1024:
            raise ValueError("参考文件单个不能超过 128 KB，总大小不能超过 256 KB")
        if any("\0" in item.content for item in self.reference_files):
            raise ValueError("参考文件必须为文本文件")
        return self


class ComponentConfirm(BaseModel):
    component_id: str = Field(alias="componentId")
    candidate_snapshot_digest: str = Field(alias="candidateSnapshotDigest")
    inputs: dict[str, Any] = Field(default_factory=dict)


class ConversationRunCreate(BaseModel):
    input: dict[str, Any]
    output: dict[str, Any]
    trajectory: dict[str, Any]


class DshTaskAction(BaseModel):
    """一个成功的 DSH 工具名称，不携带任务实例数据。"""

    name: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9_.:/-]+$",
    )


class DshTaskDistillationCreate(BaseModel):
    """草稿固化桥接仅接收的最小 DSH 会话事实。"""

    session_id: str = Field(alias="sessionId", min_length=1, max_length=160)
    actions: list[DshTaskAction] = Field(min_length=1, max_length=32)


class ContractPatchApply(BaseModel):
    node_id: str = Field(alias="nodeId")
    patch: dict[str, Any]


class AgentRunCreate(BaseModel):
    content: str = Field(min_length=1)
    allowed_capabilities: list[str] | None = Field(default=None, alias="allowedCapabilities")
    max_steps: int | None = Field(default=None, alias="maxSteps", ge=1, le=32)


class WorkflowSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=5, ge=1, le=8)
    session_id: str = Field(alias="sessionId", min_length=1)


class WorkflowDescribeRequest(BaseModel):
    ref: str = Field(min_length=1, max_length=300)
    session_id: str = Field(alias="sessionId", min_length=1)


class WorkflowSliceRequest(BaseModel):
    workflow_ref: str = Field(alias="workflowRef", min_length=1, max_length=300)
    candidate_node_ids: list[str] = Field(alias="candidateNodeIds", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)


class PlanCreate(BaseModel):
    plan: dict[str, Any]
    created_by_conversation_run_id: str | None = Field(
        default=None, alias="createdByConversationRunId"
    )
    ttl_hours: int = Field(default=168, alias="ttlHours", ge=1, le=8760)
    session_id: str = Field(alias="sessionId", min_length=1)


class DisclosureSessionCreate(BaseModel):
    conversation_id: str | None = Field(default=None, alias="conversationId")
    agent_run_id: str | None = Field(default=None, alias="agentRunId")
    ttl_minutes: int = Field(default=60, alias="ttlMinutes", ge=1, le=1440)


class PlanRunCreate(BaseModel):
    input: dict[str, Any]


class PlanSolidifyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ConversationTitleUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class ConversationDirectoryUpdate(BaseModel):
    working_directory: str | None = Field(alias="workingDirectory", max_length=4096)


class ChatWorkspaceUpdate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    working_directory: str | None = Field(
        default=None, alias="workingDirectory", min_length=1, max_length=4096
    )

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value
