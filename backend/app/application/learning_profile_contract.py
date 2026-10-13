"""Explain and preserve execution-profile constraints during bounded repairs."""
from copy import deepcopy

PURE_EXAMPLES = ("pure-python 样例的 setup、verify 必须省略或为空字符串，httpFixtures 必须为空。"
                 "maxLength=0 表示禁止该字段内容，不是要求缩短断言；使用 expected 表达输出断言。"
                 "纯链接/格式化不能为了保留 verify 改成 local-command。")


def profile_validation_feedback(error, output, data):
    if not isinstance(output, dict):
        return str(error)
    previous = data.get("previousDraft", {})
    if not isinstance(previous, dict):
        previous = {}
    candidate = output.get("skillx", output)
    if not isinstance(candidate, dict):
        return str(error)
    profile = candidate.get("executionProfile") or previous.get("executionProfile")
    examples = candidate.get("examples", [])
    if not isinstance(examples, list):
        return str(error)
    if profile == "pure-python" and any(
        isinstance(example, dict) and (example.get("setup") or example.get("verify") or example.get("httpFixtures"))
        for example in examples
    ):
        return str(error) + "\n" + PURE_EXAMPLES
    return str(error)


def bind_skillx_repair_profile(schema, data):
    """A patch inherits its base profile; validate examples against that contract."""
    schema = deepcopy(schema)
    profile = data.get("previousDraft", {}).get("executionProfile")
    condition = {"properties": {"executionProfile": {"enum": [None, "pure-python"]}}}
    if profile != "pure-python":
        condition = {"required": ["executionProfile"], "properties": {"executionProfile": {"const": "pure-python"}}}
    schema.setdefault("allOf", []).append({"if": condition, "then": {"properties": {
        "examples": {"anyOf": [{"type": "null"}, {"type": "array", "items": {"properties": {
            "setup": {"maxLength": 0, "description": PURE_EXAMPLES},
            "verify": {"maxLength": 0, "description": PURE_EXAMPLES},
            "httpFixtures": {"maxItems": 0},
            "expectedError": {"type": "null"},
            "failureOutput": {"type": "null"},
        }}}]}
    }}})
    return schema


def examples_only_error(error):
    prefix = "Agent 输出不符合 outputSchema: "
    first_line = str(error).split("\n", 1)[0]
    if not first_line.startswith(prefix):
        return False
    errors = first_line[len(prefix):].split(";")
    return bool(errors) and all(item.strip().startswith("skillx.examples") for item in errors)


def bind_examples_only_repair(schema, data):
    if not examples_only_error(data.get("validationFeedback", "")):
        return schema
    previous = data.get("previousDraft", {})
    if not previous.get("source"):
        return schema
    schema = deepcopy(schema)
    for field in ("source", "executionProfile", "inputSchema", "outputSchema"):
        if field in previous:
            schema["properties"][field] = {"type":"null",
                "description":"错误仅在examples，已通过结构校验的源码与契约不可改写；请省略此字段"}
    schema["required"] = list(dict.fromkeys([*schema.get("required", []), "examples"]))
    return schema


def validate_examples_only_patch(changes, previous, error):
    if examples_only_error(error):
        for field in ("source", "executionProfile", "inputSchema", "outputSchema"):
            if field in changes and changes[field] != previous.get(field):
                raise ValueError("仅examples格式错误，修复不可改写已正确的源码或契约：" + field)
