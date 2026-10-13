import pytest

from app.application.learning_contracts import ContractExample
from app.application.learning_counterexamples import capture_counterexample, validate_counterexamples


def example(**extra):
    return ContractExample(input={'source':'${fixture.source}'}, expected={'verified':True},
        setup='fixture = prepare_named_source()', verify='assert result["verified"]', **extra)


def test_failed_scenario_cannot_be_replaced_with_other_input_or_fixture():
    original = example()
    obligation = capture_counterexample(original, ValueError('AssertionError'))
    for replacement in [original.model_copy(update={'input':{'source':'absolute-path'}}),
                        original.model_copy(update={'setup':'fixture = prepare_absolute_source()'})]:
        with pytest.raises(ValueError, match='丢失已失败'):
            validate_counterexamples([replacement], [obligation])


def test_verifier_can_be_corrected_without_removing_business_facts():
    original = example()
    obligation = capture_counterexample(original, ValueError('业务断言失败'))
    corrected = original.model_copy(update={'verify':'assert result["verified"] is True\nassert inputs["source"]'})
    validate_counterexamples([corrected], [obligation])
    with pytest.raises(ValueError, match='撤销已声明'):
        validate_counterexamples([corrected.model_copy(update={'expected':{}})], [obligation])


@pytest.mark.parametrize('error', ['NameError', 'SyntaxError', 'IndentationError', 'UnboundLocalError'])
def test_broken_verifier_is_not_frozen_as_business_counterexample(error):
    assert capture_counterexample(example(), ValueError(error)) is None
