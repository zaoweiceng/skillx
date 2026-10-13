from types import SimpleNamespace
import pytest
from jsonschema import Draft202012Validator
from app.application.learning_contracts import ContractExample
from app.application.learning_evidence import validate_examples


def example(mode='derived', **kwargs):
    return ContractExample(input={'branch': 'feature/new'}, expected={'url': 'https://host/new'},
                           sourceInvocationId='call', evidenceMode=mode, **kwargs)


def evidence(status='succeeded'):
    return {'call': SimpleNamespace(kind='tool', status=status, output_json={'stdout': 'historical task text'})}


def other():
    return ContractExample(input={'branch': 'fix/next'}, expected={'url': 'https://host/next'})


def test_derived_provenance_does_not_claim_replay_of_original_tool_envelope():
    validate_examples([example(), other()], evidence())
    with pytest.raises(ValueError, match='样例输出与原始证据不同'):
        validate_examples([example('replay'), other()], evidence())


@pytest.mark.parametrize('case', ['missing', 'failed', 'duplicate', 'false_projection'])
def test_derived_still_requires_scoped_success_distinct_inputs_and_truthful_mappings(case):
    examples, calls = [example(), other()], evidence()
    if case == 'missing': calls = {}
    if case == 'failed': calls = evidence('failed')
    if case == 'duplicate': examples[1] = example()
    if case == 'false_projection': examples[0] = example(sourceInput={'branch': '/absent'})
    with pytest.raises(ValueError):
        validate_examples(examples, calls)


def test_evidence_mode_schema_and_backend_agree_and_default_to_replay():
    assert ContractExample(input={}, expected={}).evidenceMode == 'replay'
    value = {'input': {}, 'expected': {}, 'evidenceMode': 'unverified'}
    assert list(Draft202012Validator(ContractExample.model_json_schema()).iter_errors(value))
    with pytest.raises(ValueError): ContractExample.model_validate(value)
    assert 'evidenceMode' not in example().portable_dump()
