import pytest
from app.application.input_form_coverage import validate_input_form_coverage, check_declared_scope_change


def test_distinct_declared_forms_require_real_coverage():
    schema = {'properties': {'target': {'anyOf': [
        {'title': 'name', 'type': 'string', 'pattern': '^[a-z]+$'},
        {'title': 'path', 'type': 'string', 'pattern': '^/'},
    ]}}}
    with pytest.raises(ValueError, match='name'):
        validate_input_form_coverage(schema, [{'target': '/a'}, {'target': '/b'}])
    validate_input_form_coverage(schema, [{'target': '/a'}, {'target': 'origin'}])
    with pytest.raises(ValueError, match='scopeChange'):
        check_declared_scope_change(schema, {}, None, {}, {})


def test_indistinguishable_labels_cannot_fake_coverage():
    schema = {'anyOf': [{'title': 'a'}, {'title': 'b'}]}
    with pytest.raises(ValueError, match='a、b'):
        validate_input_form_coverage(schema, [{}, {'other': 1}])
