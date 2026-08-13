from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

# 统一表达式语法：路径段支持 `字段` 与 `字段[整数索引]` 两种形式，例如
# `${node.output.items[0].name}`。数组整体引用直接用字段名（`${node.output.tags}`）。
# 前端 VariablePicker 生成的路径必须落在此语法内，避免出现后端无法解析的 `[]`。
_SEGMENT = r"[a-zA-Z0-9_-]+(?:\[\d+\])*"
_PATH = rf"{_SEGMENT}(?:\.{_SEGMENT})*"
REFERENCE = re.compile(rf"\$\{{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>{_PATH})}}")
EXACT_REFERENCE = re.compile(rf"^\$\{{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>{_PATH})}}$")

_INDEX = re.compile(r"^(?P<name>[a-zA-Z0-9_-]+)(?P<indices>(?:\[\d+\])*)$")
_INDEX_VALUES = re.compile(r"\[(\d+)\]")


class ExpressionError(ValueError):
    pass


def _descend(current: Any, key: str) -> Any:
    if isinstance(current, dict) and key in current:
        return current[key]
    if isinstance(current, list) and key.isdigit() and int(key) < len(current):
        return current[int(key)]
    raise ExpressionError(f"无法解析路径段 {key}")


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        matched = _INDEX.match(part)
        if not matched:
            raise ExpressionError(f"非法路径段 {part}")
        current = _descend(current, matched.group("name"))
        for index in _INDEX_VALUES.findall(matched.group("indices")):
            if not isinstance(current, list) or int(index) >= len(current):
                raise ExpressionError(f"数组索引越界 {part}")
            current = current[int(index)]
    return current


def _resolve_reference(
    root: str, path: str, run_input: dict[str, Any], outputs: dict[str, Any]
) -> Any:
    """解析单个 `${root.path}` 引用。

    非 input 根统一约定：``${node.output}`` 返回该节点完整输出对象，
    ``${node.output.<field>}`` 返回其中字段。这样固化生成的整体输出引用与逐字段引用
    使用同一语义，而不再把 ``output`` 当成一个真实字段名去查找。
    """

    source = run_input if root == "input" else outputs.get(root)
    if source is None:
        raise ExpressionError(f"节点 {root} 尚无输出")
    if root == "input":
        return _get_path(source, path)
    if path == "output":
        return source
    if path.startswith("output."):
        return _get_path(source, path.removeprefix("output."))
    return _get_path(source, path)


def resolve_value(value: Any, run_input: dict[str, Any], outputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_value(item, run_input, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(item, run_input, outputs) for item in value]
    if not isinstance(value, str):
        return deepcopy(value)

    exact = EXACT_REFERENCE.match(value)
    if exact:
        root, path = exact.group("root"), exact.group("path")
        return deepcopy(_resolve_reference(root, path, run_input, outputs))

    def replace(match: re.Match[str]) -> str:
        root, path = match.group("root"), match.group("path")
        resolved = _resolve_reference(root, path, run_input, outputs)
        # 字符串插值中的对象/数组必须显式 JSON 序列化，不依赖 Python str(dict)。
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False, sort_keys=True)
        return str(resolved)

    return REFERENCE.sub(replace, value)


def resolve_mapping(
    mapping: dict[str, Any], run_input: dict[str, Any], outputs: dict[str, Any]
) -> dict[str, Any]:
    return resolve_value(mapping, run_input, outputs)
