from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HumanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=120)
    kind: Literal["information", "choice"] = "information"
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    inputSchema: dict[str, Any]
    uiSchema: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    recommended: str | None = None

    @model_validator(mode="after")
    def valid_schema(self):
        Draft202012Validator.check_schema(self.inputSchema)
        if self.inputSchema.get("type") != "object":
            raise ValueError("交互输入必须是 object Schema")
        return self


class TaskCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=120)
    kind: Literal["capability", "tool", "code", "install", "model"]
    arguments: dict[str, Any] = Field(
        description="code 调用：{language: python|javascript, source: 字符串}；"
        "tool 调用：{capabilityRef: 字符串, input: 对象}。"
        "code 的 language 和 source 必须直接放在 arguments 中，不能包在 input 中。"
    )
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_code_arguments(self):
        if self.kind == "code":
            if (
                self.arguments.get("language") not in {"python", "javascript"}
                or not isinstance(self.arguments.get("source"), str)
                or not self.arguments["source"].strip()
            ):
                raise ValueError(
                    "code.arguments 必须直接包含 language 和非空 source，不能包在 input 中"
                )
        return self

    @model_validator(mode="before")
    @classmethod
    def legacy_call(cls, value):
        # Read old persisted/provider decisions, but advertise only one callable
        # contract in the model schema.
        from app.application.callable_capabilities import public_call

        if isinstance(value, dict) and value.get("kind") in {"workflow", "skillx"}:
            kind, arguments = public_call(value["kind"], value.get("arguments", {}))
            return {**value, "kind": kind, "arguments": arguments}
        return value


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["dispatch", "human.request", "complete", "blocked"]
    summary: str
    completionCriteria: list[str] = Field(min_length=1)
    remaining: list[str] = Field(default_factory=list)
    calls: list[TaskCall] = Field(default_factory=list, max_length=8)
    questions: list[HumanRequest] = Field(default_factory=list, max_length=3)
    evidence: list[str] = Field(default_factory=list)
    answer: str = ""

    @model_validator(mode="after")
    def consistent(self):
        if self.action == "dispatch" and not self.calls:
            raise ValueError("dispatch 需要 calls")
        if self.action == "human.request" and not self.questions:
            raise ValueError("human.request 需要 questions")
        if self.action in {"complete", "blocked"} and (self.calls or self.questions):
            raise ValueError("终态禁止同时派发")
        if self.action == "complete" and (self.remaining or not self.answer):
            raise ValueError("完成时必须汇报且无剩余事项")
        keys = [x.key for x in self.calls] + [x.key for x in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("调用与问题 key 必须唯一")
        return self
