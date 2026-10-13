from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from app.application.dependency_reuse_contract import (
    dependency_reuse_contract,
    validate_dependency_reuse,
)


@pytest.mark.parametrize('group,key,node_type', [
    ('suppliedSkillXContracts', 'generatedSkill', 'skillx'),
    ('suppliedWorkflowContracts', 'generatedWorkflow', 'workflow_call'),
])
def test_declared_generated_dependency_must_be_called(group, key, node_type):
    data = {'fragment': {'dependsOn': ['selector']}, group: [{'name': 'selector'}]}
    value = {'decision': 'ready', 'workflow': {'spec': {'nodes': [
        {'id': 'pretend', 'type': 'transform', 'config': {'operation': 'passthrough'}},
    ]}}}
    validator = Draft202012Validator(dependency_reuse_contract(data))
    assert not validator.is_valid(value)
    with pytest.raises(ValueError, match='依赖未实际调用'):
        validate_dependency_reuse(SimpleNamespace(model_dump=lambda: value), data)
    value['workflow']['spec']['nodes'][0]['config'][key] = 'selector'
    assert not validator.is_valid(value)
    # Ordered side-effect dependencies need not have their return value consumed.
    value['workflow']['spec']['nodes'][0].update(type=node_type, config={key: 'selector'})
    validator.validate(value)
    validate_dependency_reuse(SimpleNamespace(model_dump=lambda: value), data)


def test_unused_catalog_contract_is_not_an_implied_dependency_and_skip_is_allowed():
    data = {'fragment': {'dependsOn': []}, 'suppliedSkillXContracts': [{'name': 'optional'}]}
    Draft202012Validator(dependency_reuse_contract(data)).validate(
        {'decision': 'ready', 'workflow': {'spec': {'nodes': []}}})
    data['fragment']['dependsOn'] = ['optional']
    Draft202012Validator(dependency_reuse_contract(data)).validate({'decision': 'skip'})
