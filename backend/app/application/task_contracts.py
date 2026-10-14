from copy import deepcopy
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HumanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=120)
    kind: Literal["information", "choice"] = "information"
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    inputSchema: dict[str, Any] = Field(json_schema_extra={
        "required": ["type"], "properties": {"type": {"const": "object"}},
    })
    uiSchema: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    recommended: str | None = None

    @model_validator(mode="after")
    def valid_schema(self):
        Draft202012Validator.check_schema(self.inputSchema)
        if self.inputSchema.get("type") != "object":
            raise ValueError("交互输入必须是 object Schema")
        return self


CODE_ARGUMENTS = {
    "type": "object",
    "required": ["language", "source"],
    "properties": {
        "language": {"enum": ["python", "javascript"]},
        # Equivalent under JSON Schema search and provider full-match regex engines.
        "source": {"type": "string", "pattern": r"^[\s\S]*\S[\s\S]*$"},
    },
}


def schema_variants(schema: dict, discriminator: str, rules: dict) -> None:
    # Each branch is self-contained: some providers compile anyOf independently
    # and otherwise lose constraints declared alongside it on the parent object.
    common = deepcopy(schema)
    schema["anyOf"] = []
    for value, rule in rules.items():
        properties = deepcopy(common["properties"])
        for name, constraints in rule.get("properties", {}).items():
            properties[name] = {**properties[name], **deepcopy(constraints)}
        properties[discriminator] = {"const": value} if isinstance(value, str) else {
            "enum": list(value),
        }
        schema["anyOf"].append({
            **deepcopy(common),
            "properties": properties,
            "required": list(dict.fromkeys(common.get("required", []) + rule.get("required", []))),
        })


def call_schema(schema: dict) -> None:
    schema_variants(schema, "kind", {
        "code": {"properties": {"arguments": CODE_ARGUMENTS}},
        ("capability", "tool", "install", "model"): {},
    })


class TaskCall(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=call_schema)
    key: str = Field(min_length=1, max_length=120)
    kind: Literal["capability", "tool", "code", "install", "model"]
    arguments: dict[str, Any] = Field(
        description="code 调用：{language: python|javascript, source: 字符串}；"
        "tool 调用：{capabilityRef: 字符串, input: 对象}。"
        "code 的 language 和 source 必须直接放在 arguments 中，不能包在 input 中。"
    )
    # Display metadata only; absence must not force regeneration of executable
    # arguments or invent a rationale. HumanRequest.reason remains mandatory.
    reason: str = ""

    @model_validator(mode="after")
    def valid_code_arguments(self):
        if self.kind == "code":
            if not Draft202012Validator(CODE_ARGUMENTS).is_valid(self.arguments):
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


# One source for provider-visible action constraints and backend validation.
# References/evidence and key uniqueness still require runtime semantic checks.
DECISION_RULES = {
    "dispatch": {
        "required": ["calls"],
        "properties": {"calls": {"minItems": 1}},
    },
    "human.request": {
        "required": ["questions"],
        "properties": {"questions": {"minItems": 1}},
    },
    "complete": {
        "required": ["answer"],
        "properties": {
            "answer": {"minLength": 1},
            "remaining": {"maxItems": 0},
            "calls": {"maxItems": 0},
            "questions": {"maxItems": 0},
        },
    },
    "blocked": {
        "properties": {"calls": {"maxItems": 0}, "questions": {"maxItems": 0}},
    },
}


def decision_schema(schema: dict) -> None:
    schema_variants(schema, "action", DECISION_RULES)


class LearningCandidate(BaseModel):
    """Nominate an execution-derived improvement, not a summary of source material."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["new_method", "recovered_failure"]
    novelty: str = Field(min_length=1, max_length=600)
    verification: str = Field(min_length=1, max_length=600)
    sourceInvocationIds: list[str] = Field(min_length=1, max_length=8)


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=decision_schema)
    action: Literal["dispatch", "human.request", "complete", "blocked"]
    summary: str
    completionCriteria: list[str] = Field(min_length=1)
    remaining: list[str] = Field(default_factory=list)
    calls: list[TaskCall] = Field(default_factory=list, max_length=8)
    questions: list[HumanRequest] = Field(default_factory=list, max_length=3)
    evidence: list[str] = Field(default_factory=list)
    answer: str = ""
    evidenceGap: str = Field(default="", max_length=500, description=(
        "已有执行证据后继续 dispatch 时必填：指出用户问题中尚不能回答的具体事实、"
        "已有原文为何不能回答，以及本次调用如何补齐。仅因文档提到其他文件或想再次确认，不构成缺口。"
    ))
    learningCandidate: LearningCandidate | None = Field(default=None, description=(
        "仅完成任务时提名经执行验证的新方法或修复经验；普通检索、文档解读、复述已有 Skill、"
        "现有工具的常规使用返回 null。novelty 说明相对既有方法新增了什么，"
        "verification 说明执行如何验证。"
    ))

    @model_validator(mode="after")
    def consistent(self):
        errors = list(Draft202012Validator(DECISION_RULES[self.action]).iter_errors(
            self.model_dump()
        ))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.path) or "decision"
            raise ValueError(f"{self.action}.{path}: {error.message}")
        keys = [x.key for x in self.calls] + [x.key for x in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("调用与问题 key 必须唯一")
        return self


def task_decision_schema(
    calls: list[dict], state: dict | None = None, *, current_call_ids: set[str] | None = None,
) -> dict:
    """Bind runtime evidence to the authoritative Host validation contract.

    Pi projects this into stable provider tools; state-dependent membership and
    follow-up requirements are still enforced here after generation.
    """
    schema = TaskDecision.model_json_schema()
    # Transport metadata, not a relaxation of the canonical decision contract.
    # Pi presents one small tool per action; stable control fields need not be
    # regenerated while the model is answering the user.
    schema["x-task-decision"] = {
        "completeDefaults": {
            "summary": "任务完成",
            "completionCriteria": (state or {}).get("completionCriteria") or ["完成用户目标"],
        },
    }
    ids = [call["id"] for call in calls if call["status"] == "succeeded"]
    # Historical evidence is legal to cite, but an unrelated follow-up must not
    # be forced to cite it. Existing callers retain their task-local semantics.
    requires_evidence = bool(ids if current_call_ids is None else set(ids) & current_call_ids)
    evidence = {"type": "array", "uniqueItems": True, "maxItems": len(ids),
                "items": {"type": "string", "enum": ids} if ids else {"type": "string"}}
    schema["properties"]["evidence"] = evidence
    for branch in schema["anyOf"]:
        branch["properties"]["evidence"] = deepcopy(evidence)
        action = branch["properties"]["action"]["const"]
        if action == "complete" and requires_evidence:
            branch["required"].append("evidence")
            branch["properties"]["evidence"]["minItems"] = 1
        if action == "dispatch" and requires_evidence:
            branch["required"].append("evidenceGap")
            branch["properties"]["evidenceGap"]["minLength"] = 1
    return schema
