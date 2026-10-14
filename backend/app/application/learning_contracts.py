"""Typed artifacts shared by background learning agents and deterministic publishers."""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, model_validator, field_validator


def validate_python_source(source: str) -> str:
    import ast
    if not source.strip():
        raise ValueError("direct 能力必须提供完整 source，实现 execute(request, context)，从 request.input 读取参数")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"source Python 语法错误：{exc}") from exc
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "execute" for node in tree.body):
        raise ValueError("source 必须定义顶层 execute(request, context) 入口")
    return source


# Python is a language, not a prefix regex. Keep transport constraints structural;
# validate semantics with the same parser for drafts, completion and repairs.
PythonSource = Annotated[str, Field(min_length=1, max_length=100000,
    description="完整 Python 模块，定义顶层 execute(request, context)，通过 request.input 读取参数并返回业务结果。"),
    AfterValidator(validate_python_source)]


JSONType = Literal["object", "array", "string", "number", "integer", "boolean", "null"]


class BusinessSchemaDefinition(BaseModel):
    """Describe schema-valued fields as schemas, not arbitrary JSON strings."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    type: JSONType | Annotated[list[JSONType], Field(min_length=1)] | None = None
    properties: dict[str, "BusinessSchemaDefinition | bool"] | None = None
    required: list[str] | None = Field(default=None, json_schema_extra={"uniqueItems": True})
    enum: list[Any] | None = Field(default=None, min_length=1)
    items: "BusinessSchemaDefinition | bool | None" = None
    additionalProperties: "BusinessSchemaDefinition | bool | None" = None
    definitions: dict[str, "BusinessSchemaDefinition | bool"] | None = Field(default=None, alias="$defs")
    anyOf: list["BusinessSchemaDefinition | bool"] | None = Field(default=None, min_length=1)
    allOf: list["BusinessSchemaDefinition | bool"] | None = Field(default=None, min_length=1)
    oneOf: list["BusinessSchemaDefinition | bool"] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def omit_absent_keywords(self):
        # Null on an optional schema keyword means not supplied. Preserve actual
        # JSON null values in extra keywords such as const and enum.
        for name in type(self).model_fields:
            if getattr(self, name) is None:
                self.__pydantic_fields_set__.discard(name)
        return self


def validate_business_schema(value):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    result = value.model_dump(by_alias=True, exclude_unset=True)
    try:
        Draft202012Validator.check_schema(result)
    except SchemaError as exc:
        path = ".".join(str(part) for part in exc.path) or "$"
        raise ValueError(f"业务 JSON Schema 无效：{path}: {exc.message}") from exc
    return result


BusinessSchema = Annotated[BusinessSchemaDefinition,
    AfterValidator(validate_business_schema), PlainSerializer(lambda value: value)]


class BusinessObjectSchema(BusinessSchemaDefinition):
    type: Literal["object"]
    properties: dict[str, BusinessSchemaDefinition | bool]


GeneratedBusinessSchema = Annotated[BusinessObjectSchema,
    AfterValidator(validate_business_schema), PlainSerializer(lambda value: value)]


EvidencePointer = Annotated[str, Field(pattern=r"^(?:|/(?:[^~]|~[01])*)$",
    description="JSON Pointer 相对于 calls[].observedOutput：空字符串表示整个结果，/field 表示字段；不是字段的字面值。")]


class HttpFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    url: str = Field(pattern=r"^https?://", description="完整合成 URL，必须与候选实际请求精确匹配")
    requestJson: Any = Field(default=None, description="JSON 请求体；null 表示无请求体，按 JSON 值精确匹配")
    responseJson: Any
    prepare: str = Field(default="", max_length=16384, description="可选的请求时独立 Python 准备脚本；在既有隔离目录读取当前运行状态，修改 requestJson/responseJson 变量。不给实际请求或候选模块，仍按完整请求精确匹配；不得调用候选实现生成预期。5 秒上限。")
    status: int = Field(default=200, ge=100, le=599)


class ContractExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict
    expected: dict
    sourceInvocationId: str | None = None
    evidenceMode: Literal["replay", "derived"] = Field(default="replay", description="replay 重放原结果或显式映射；derived 为成功来源启发的新业务断言，仍必须隔离执行验证。")
    # JSON pointers into the observed output; all business input fields must be grounded.
    sourceInput: dict[str, EvidencePointer] | None = None
    sourceOutput: dict[str, EvidencePointer] | None = None
    setup: str = Field(default="", max_length=20000, description="临时环境准备 Python；动态值可通过 stdout 输出一个完整 JSON 对象，或写 .skillx-fixture.json。通过 ${fixture.key} 引用，两通道同名值必须一致。")
    verify: str = Field(default="", max_length=20000)
    httpFixtures: list[HttpFixture] = Field(default_factory=list, max_length=64, description="隔离验证专用：顺序匹配 urllib.request.urlopen 的 method/URL/JSON body；所有 fixture 必须被实际调用消费，禁止真实网络")

    def portable_dump(self):
        return self.model_dump(exclude={"sourceInvocationId", "sourceInput", "sourceOutput", "evidenceMode"})


class ExpectedExecutionError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1)
    messageContains: str = Field(min_length=1)


class SkillXExample(ContractExample):
    failureOutput: dict | None = Field(default=None, description="HTTP 失败但按契约返回结构化结果时，声明业务失败输出子集（如成功标志=false或非空错误原因），不能仅提供HTTP状态码；Host独立核验。使用expectedError时省略。")
    expectedError: ExpectedExecutionError | None = Field(default=None,
        description="仅 local-command 的预期拒绝用例：匹配执行异常类型与消息，verify 用 error 检查异常并断言仓库未被误改。正常样例省略。")


class SkillXProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "allOf": [{
            "if": {"properties": {"kind": {"const": "direct"}}},
            "then": {"required": ["source"],
                     "properties": {"source": {"type": "string", "minLength": 1}}},
            "else": {"required": ["prompt"],
                     "properties": {"prompt": {"type": "string", "minLength": 1}}},
        }],
    })
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(min_length=1)
    inputSchema: BusinessSchema
    outputSchema: BusinessSchema
    kind: Literal["direct", "llm", "agent"] = "direct"
    executionProfile: Literal["pure-python", "local-command"] = "pure-python"
    prompt: str = Field(default="", max_length=100000)
    dependencies: dict = Field(default_factory=dict)
    source: str = Field(default="", max_length=100000)
    examples: list[SkillXExample] = Field(min_length=2, max_length=8)

    @field_validator("examples", mode="before")
    @classmethod
    def example_values(cls, values):
        return [value.model_dump() if isinstance(value, ContractExample) else value for value in values]

    @model_validator(mode="after")
    def executable_contract(self):
        if self.kind == "direct":
            validate_python_source(self.source)
            if self.executionProfile == "pure-python" and any(e.setup or e.verify or e.httpFixtures for e in self.examples):
                raise ValueError("pure-python 的 setup/verify/httpFixtures 必须为空；使用 expected 声明输出断言，不是缩短 verify，也不能为纯断言改成 local-command")
        elif not self.prompt.strip():
            raise ValueError("模型能力必须提供非空 prompt")
        if any(e.expectedError for e in self.examples) and (self.kind != "direct" or self.executionProfile != "local-command"):
            raise ValueError("预期执行错误样例仅支持 local-command")
        if not any(e.expectedError is None and not e.failureOutput for e in self.examples):
            raise ValueError("至少一个样例必须验证成功执行，不能仅靠拒绝输入发布能力")
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
        def dynamic(value):
            if isinstance(value, str):
                return value.startswith("${fixture.") and value.endswith("}")
            if isinstance(value, dict):
                return any(dynamic(item) for item in value.values())
            if isinstance(value, list):
                return any(dynamic(item) for item in value)
            return False
        for index, example in enumerate(self.examples):
            if dynamic(example.input):
                continue  # Validated after setup resolves the actual value.
            try:
                Draft202012Validator(self.inputSchema).validate(example.input)
            except ValidationError as exc:
                raise ValueError(f"examples[{index}].input 与 inputSchema 不一致：{exc.message}") from exc
        return self


class DirectSkillXProposal(SkillXProposal):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "allOf": [{"properties": {"examples": {"contains": {"properties": {"expectedError": {"type": "null"}, "failureOutput": {"type": "null"}}}}}}, {
        "if": {"properties": {"executionProfile": {"const": "local-command"}}},
        "then": {"properties": {"examples": {"items": {
            "required": ["setup", "verify"],
            "properties": {"setup": {"pattern": r"\S"}, "verify": {"pattern": r"\S"}}}}}},
        "else": {"properties": {"examples": {"items": {
            "properties": {"setup": {"maxLength": 0}, "verify": {"maxLength": 0},
                           "expectedError": {"type": "null"}}}}}},
    }]})
    kind: Literal["direct"]
    executionProfile: Literal["pure-python", "local-command"]
    inputSchema: GeneratedBusinessSchema
    outputSchema: GeneratedBusinessSchema
    source: PythonSource

    @model_validator(mode="after")
    def local_fixtures(self):
        if self.executionProfile == "local-command" and any(
            not example.setup.strip() or not example.verify.strip() for example in self.examples
        ):
            raise ValueError("local-command 的每个样例必须提供非空 setup 和 verify")
        return self


class ModelSkillXProposal(SkillXProposal):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [{"properties": {
        "examples": {"items": {"properties": {"expectedError": {"type": "null"}}}}}}]})
    kind: Literal["agent", "llm"]
    prompt: str = Field(min_length=1, max_length=100000)


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


class WorkflowProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    definition: dict
    scope: str
    sourceInvocationIds: list[str]
    dependsOn: list[str] = Field(default_factory=list)
    examples: list[ContractExample] = Field(default_factory=list)



class LearningProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(skip|candidate)$")
    reason: str
    skills: list[InstructionSkillProposal] = Field(default_factory=list, max_length=8)
    skillx: list[SkillXProposal] = Field(default_factory=list, max_length=8)
    workflows: list[WorkflowProposal] = Field(default_factory=list, max_length=16)
    failures: dict[str, str] = Field(default_factory=dict)
    workflow: dict | None = None
    workflowScope: str = ""
    workflowSourceInvocationIds: list[str] = Field(default_factory=list, max_length=32)
    baseVersionId: str | None = None
    workflowExamples: list[ContractExample] = Field(default_factory=list, max_length=8)
