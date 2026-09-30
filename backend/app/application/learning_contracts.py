"""Typed artifacts shared by background learning agents and deterministic publishers."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict
    expected: dict
    sourceInvocationId: str | None = None


class SkillXProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(min_length=1)
    inputSchema: dict
    outputSchema: dict
    kind: Literal["direct", "llm", "agent"] = "direct"
    prompt: str = Field(default="", max_length=100000)
    dependencies: dict = Field(default_factory=dict)
    source: str = Field(default="", max_length=100000)
    examples: list[ContractExample] = Field(min_length=2, max_length=8)


# Compatibility for materializers importing the original executable proposal type.
SkillProposal = SkillXProposal


class InstructionSkillProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=120)
    displayName: str = Field(default="", max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=100000)
    useWhen: list[str] = Field(min_length=1, max_length=8)
    sourceInvocationIds: list[str] = Field(min_length=1, max_length=32)


class LearningProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(skip|candidate)$")
    reason: str
    skills: list[InstructionSkillProposal] = Field(default_factory=list, max_length=8)
    skillx: list[SkillXProposal] = Field(default_factory=list, max_length=8)
    workflow: dict | None = None
    workflowScope: str = ""
    workflowSourceInvocationIds: list[str] = Field(default_factory=list, max_length=32)
    baseVersionId: str | None = None
    workflowExamples: list[ContractExample] = Field(default_factory=list, max_length=8)
