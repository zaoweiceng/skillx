"""Ground examples in recorded results without confusing raw and derived contracts."""
from app.application.compiler import digest_json


def project(output, mapping, *, context=""):
    def resolve_pointer(value, pointer):
        if pointer == "":
            return value
        if not pointer.startswith("/"):
            raise ValueError("必须使用 JSON Pointer")
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            try:
                if isinstance(value, list):
                    if not part.isdigit() or (len(part) > 1 and part.startswith("0")):
                        raise IndexError(part)
                    value = value[int(part)]
                else:
                    value = value[part]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                available = (f"对象字段={list(value)[:20]}" if isinstance(value, dict)
                             else f"数组长度={len(value)}" if isinstance(value, list)
                             else f"当前类型={type(value).__name__}（不能按字段继续访问）")
                raise ValueError(f"证据映射路径不存在：pointer={pointer!r}，失败段={part!r}，{available}") from exc
        return value
    if not mapping:
        raise ValueError("证据映射不能为空")
    result = {}
    for key, pointer in mapping.items():
        try:
            result[key] = resolve_pointer(output, pointer)
        except ValueError as exc:
            raise ValueError(f"{context}[{key!r}]: {exc}；映射根是 calls[].observedOutput，不能使用字面值或猜测字段") from exc
    return result


def validate_examples(examples, evidence, *, local_command=False):
    from app.application.task_learning import observed_output
    # For local commands repository/file state is part of the effective input.
    # The same repoPath in two independently initialized repositories is not a
    # duplicate case (e.g. clean vs dirty tree). Pure transformations use JSON only.
    inputs = [{"input": item.input, "setup": item.setup.strip()} if local_command else item.input
              for item in examples]
    if len({digest_json(value) for value in inputs}) < 2:
        raise ValueError("必须提供至少两个不同输入或独立环境状态的业务断言")
    # Local fixtures deliberately differ from the original repository. Their
    # provenance is the Host-scoped fragment evidence, not a model-copied UUID.
    grounded = local_command and any(call.status == "succeeded" for call in evidence.values())
    for index, item in enumerate(examples):
        if local_command and (not item.setup.strip() or not item.verify.strip()):
            raise ValueError("命令验证必须使用独立临时环境与后置条件")
        if not item.sourceInvocationId:
            if item.sourceInput is not None or item.sourceOutput is not None:
                raise ValueError("证据映射必须指定来源调用")
            continue
        call = evidence.get(item.sourceInvocationId)
        if call is None or call.status != "succeeded":
            raise ValueError("样例来源调用不存在或未成功")
        if local_command:
            # The original call anchors the use case, not the SHA/path of a newly
            # created test repository. Actual postconditions are executed in isolation.
            grounded = True
            continue
        output = observed_output(call)
        if item.sourceInput is not None:
            # Transformation contracts consume recorded data, rather than reproducing
            # the enclosing tool response. Their expected value is checked by execution.
            if project(output, item.sourceInput, context=f"examples[{index}].sourceInput (sourceInvocationId={item.sourceInvocationId})") != item.input:
                raise ValueError("样例输入与来源证据映射不一致")
        if item.sourceOutput is not None:
            if project(output, item.sourceOutput, context=f"examples[{index}].sourceOutput (sourceInvocationId={item.sourceInvocationId})") != item.expected:
                raise ValueError("样例输出与来源证据映射不一致")
        elif item.evidenceMode == "replay" and item.sourceInput is None and output != item.expected:
            raise ValueError("样例输出与原始证据不同；转换能力须提供 sourceInput，投影输出须提供 sourceOutput")
        grounded = True
    if not grounded:
        raise ValueError("缺少关联原始成功调用的业务样例")
