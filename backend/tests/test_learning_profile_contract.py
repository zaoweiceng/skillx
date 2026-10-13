from jsonschema import Draft202012Validator

from app.application.learning_agents import SkillXRepair
from app.application.learning_profile_contract import bind_skillx_repair_profile, profile_validation_feedback


def test_repair_examples_inherit_pure_profile_but_allow_explicit_real_io():
    schema = bind_skillx_repair_profile(SkillXRepair.model_json_schema(), {'previousDraft': {'executionProfile':'pure-python'}})
    validator = Draft202012Validator(schema)
    examples = [{'input':{},'expected':{},'verify':'assert result == {}'}] * 2
    assert list(validator.iter_errors({'examples':examples}))
    assert list(validator.iter_errors({'executionProfile':None,'examples':examples}))
    assert not list(validator.iter_errors({'examples':[{'input':{},'expected':{}}]*2}))
    assert not list(validator.iter_errors({'executionProfile':'local-command','examples':examples}))


def test_profile_feedback_explains_zero_length_instead_of_shortening():
    patch = {'examples':[{'input':{},'expected':{},'verify':'assert True'}]}
    message = profile_validation_feedback('verify: maxLength', patch, {'previousDraft':{'executionProfile':'pure-python'}})
    assert '不是要求缩短' in message
    assert 'expected' in message
    assert patch['examples'][0]['verify'] == 'assert True'  # No Host rewrite.
    assert profile_validation_feedback('other', patch, {'previousDraft':{'executionProfile':'local-command'}}) == 'other'


def test_pi_submit_result_preserves_repair_profile_constraints():
    import json
    import subprocess
    from pathlib import Path
    schema = bind_skillx_repair_profile(SkillXRepair.model_json_schema(), {'previousDraft': {'executionProfile':'pure-python'}})
    script = """
import { resultTools } from './bridges/pi/result-tools.mjs';
import { readFileSync } from 'node:fs';
console.log(JSON.stringify(resultTools(JSON.parse(readFileSync(0, 'utf8')))[0]));
"""
    tool = json.loads(subprocess.run(['node','--input-type=module','-e',script], input=json.dumps(schema), text=True, capture_output=True, check=True, cwd=Path(__file__).parents[2]).stdout)
    assert tool['name'] == 'submit_result'
    assert tool['parameters'] == schema
    assert not Draft202012Validator(tool['parameters']).is_valid({'examples':[{'input':{},'expected':{},'verify':'assert True'}]*2})


def test_profile_feedback_preserves_null_or_invalid_examples_error():
    for examples in (None, 3, "invalid"):
        assert profile_validation_feedback("original validation error", {"examples":examples}, {"previousDraft":{"executionProfile":"pure-python"}}) == "original validation error"


def test_examples_only_repair_cannot_drop_protocol_source_fields():
    import pytest
    from app.application.learning_profile_contract import bind_examples_only_repair, validate_examples_only_patch
    original = {'source':'def execute(request, context): return {"required_metadata": []}', 'executionProfile':'local-command', 'inputSchema':{'type':'object'}, 'outputSchema':{'type':'object'}}
    error = 'Agent 输出不符合 outputSchema: skillx.examples.0: required ["verify"]; skillx.examples: minItems 2'
    schema = bind_examples_only_repair(SkillXRepair.model_json_schema(), {'previousDraft':original, 'validationFeedback':error})
    examples = [{'input':{},'expected':{}},{'input':{'x':1},'expected':{}}]
    assert Draft202012Validator(schema).is_valid({'examples':examples})
    changed = {'source':'def execute(request, context): return {}', 'examples':examples}
    assert not Draft202012Validator(schema).is_valid(changed)
    with pytest.raises(ValueError, match='不可改写'):
        validate_examples_only_patch(changed, original, error)
    validate_examples_only_patch(changed, original, '候选实现执行错误')  # Execution failure can require source repair.


def test_external_write_protocol_evidence_survives_context_compaction():
    from app.application.learning_agents import compact_builder_context
    source = 'request_payload = ' + repr({'metadata': 'x'*2000, 'required_array':[]})
    call = {'id':'source','kind':'code','status':'succeeded','input':{'source':source},'output':{},'observedOutput':{'accepted':True}}
    result = compact_builder_context({'fragment':{'requiredEffects':['http.write']},'executionProfile':'local-command','calls':[call]})
    assert result['calls'][0]['input']['source'] == source
    assert result['calls'][0]['observedOutput'] == {'accepted':True}


def test_examples_only_detection_ignores_absent_feedback():
    from app.application.learning_profile_contract import examples_only_error, bind_examples_only_repair
    assert not examples_only_error("")
    assert bind_examples_only_repair({"properties":{}}, {}) == {"properties":{}}
