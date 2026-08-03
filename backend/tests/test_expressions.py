import pytest

from app.application.expressions import ExpressionError, resolve_mapping, resolve_value


def test_resolves_input_and_node_object_paths() -> None:
    run_input = {"name": "Alice", "profile": {"age": 30}}
    outputs = {"format": {"text": "hi", "user": {"name": "Bob"}}}
    assert resolve_value("${input.name}", run_input, outputs) == "Alice"
    assert resolve_value("${input.profile.age}", run_input, outputs) == 30
    assert resolve_value("${format.output.user.name}", run_input, outputs) == "Bob"


def test_resolves_array_whole_and_indexed() -> None:
    outputs = {"format": {"tags": ["a", "b", "c"], "rows": [{"v": 1}, {"v": 2}]}}
    # 整体数组引用。
    assert resolve_value("${format.output.tags}", {}, outputs) == ["a", "b", "c"]
    # 索引引用（前端 VariablePicker 生成 [0]）。
    assert resolve_value("${format.output.tags[0]}", {}, outputs) == "a"
    assert resolve_value("${format.output.rows[1].v}", {}, outputs) == 2


def test_interpolation_within_string() -> None:
    outputs = {"n": {"city": "成都"}}
    assert resolve_value("城市：${n.output.city}", {}, outputs) == "城市：成都"


def test_out_of_range_index_fails() -> None:
    outputs = {"format": {"tags": ["a"]}}
    with pytest.raises(ExpressionError):
        resolve_value("${format.output.tags[5]}", {}, outputs)


def test_unknown_path_fails() -> None:
    with pytest.raises(ExpressionError):
        resolve_value("${format.output.missing}", {}, {"format": {}})


def test_mapping_recurses() -> None:
    outputs = {"a": {"x": 1}}
    mapping = {"nested": {"value": "${a.output.x}"}, "list": ["${a.output.x}"]}
    assert resolve_mapping(mapping, {}, outputs) == {"nested": {"value": 1}, "list": [1]}
