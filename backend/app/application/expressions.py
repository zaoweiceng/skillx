from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

REFERENCE = re.compile(r"\$\{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>[\w.-]+)}")
EXACT_REFERENCE = re.compile(r"^\$\{(?P<root>input|[a-zA-Z][\w-]*)\.(?P<path>[\w.-]+)}$")


class ExpressionError(ValueError):
    pass


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ExpressionError(f"无法解析路径 {path}")
    return current


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
        source = run_input if root == "input" else outputs.get(root)
        if source is None:
            raise ExpressionError(f"节点 {root} 尚无输出")
        if root != "input" and path.startswith("output."):
            path = path.removeprefix("output.")
        return deepcopy(_get_path(source, path))

    def replace(match: re.Match[str]) -> str:
        root, path = match.group("root"), match.group("path")
        source = run_input if root == "input" else outputs.get(root)
        if source is None:
            raise ExpressionError(f"节点 {root} 尚无输出")
        if root != "input" and path.startswith("output."):
            path = path.removeprefix("output.")
        return str(_get_path(source, path))

    return REFERENCE.sub(replace, value)


def resolve_mapping(
    mapping: dict[str, Any], run_input: dict[str, Any], outputs: dict[str, Any]
) -> dict[str, Any]:
    return resolve_value(mapping, run_input, outputs)
