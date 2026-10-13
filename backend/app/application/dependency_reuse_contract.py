"""Explicit Agent-selected replacements for successfully skipped extraction fragments."""
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field


class ReusedDependency(BaseModel):
    model_config = ConfigDict(extra='forbid')
    versionId: str = Field(min_length=1)
    interface: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    reason: str = Field(pattern=r'\S')


def dependency_reuse_contract(data):
    declared = set(data.get('fragment', {}).get('dependsOn', []))
    generated_rules = []
    for group, key, node_type in (
        ('suppliedSkillXContracts', 'generatedSkill', 'skillx'),
        ('suppliedWorkflowContracts', 'generatedWorkflow', 'workflow_call'),
    ):
        for item in data.get(group, []):
            if item['name'] not in declared:
                continue
            generated_rules.append({'contains': {
                'required': ['type', 'config'],
                'properties': {'type': {'const': node_type}, 'config': {
                    'required': [key], 'properties': {key: {'const': item['name']}}}},
            }})
    skipped = {item['name'] for item in data.get('skippedDependencies', [])}
    pinned = {item['name']: item['versionId'] for item in data.get('skippedDependencies', []) if item.get('versionId')}
    choices, node_rules = [], []
    choices_by_name = {name: [] for name in skipped}
    for item in data.get('catalog', {}).get('capabilities', []):
        for interface in item.get('interfaces', {}):
            if not item.get('digest'):
                continue
            lock = {'versionId': item['id'], 'interface': interface, 'digest': item['digest']}
            selector = {'type': 'object', 'required': list(lock),
                        'properties': {key: {'const': value} for key, value in lock.items()}}
            choices.append(selector)
            call = {'required': ['type', 'config'], 'properties': {'type': {'const': 'capability_call'},
                'config': {'required': ['capabilityVersionId', 'interface', 'digest'], 'properties': {
                    'capabilityVersionId': {'const': item['id']}, 'interface': {'const': interface},
                    'digest': {'const': item['digest']}}}}}
            nested = {'required': ['type', 'config'], 'properties': {'type': {'const': 'workflow_call'},
                'config': {'required': ['workflowVersionId', 'digest'], 'properties': {
                    'workflowVersionId': {'const': item['id']}, 'digest': {'const': item['digest']}}}}}
            actual_nodes = {'contains': {'anyOf': [call, nested] if interface == 'execute' else [call]}}
            actual_workflow = {'properties': {'spec': {'properties': {'nodes': actual_nodes}}}}
            for name in skipped:
                if name in pinned and pinned[name] != item['id']:
                    continue
                choices_by_name[name].append(selector)
                selection = {'required': [name], 'properties': {name: selector}}
                node_rules.append({
                    'if': {'required': ['reusedDependencies'], 'properties': {'reusedDependencies': selection}},
                    'then': {'required': ['workflow'], 'properties': {'workflow': actual_workflow}},
                })
    required = {'properties': {'reusedDependencies': {'type': 'object', 'additionalProperties': False,
        'required': sorted(skipped), 'properties': {name: {'anyOf': choices_by_name[name] or [False]} for name in skipped}}}}
    if skipped:
        required['required'] = ['reusedDependencies']
    if generated_rules:
        required.setdefault('required', []).append('workflow')
        required['properties']['workflow'] = {'required': ['spec'], 'properties': {
            'spec': {'required': ['nodes'], 'properties': {'nodes': {'allOf': generated_rules}}}}}
    return {'allOf': [{'if': {'required': ['decision'], 'properties': {'decision': {'const': 'ready'}}},
                     'then': required}, *node_rules]}


def validate_dependency_reuse(value, data):
    errors = list(Draft202012Validator(dependency_reuse_contract(data)).iter_errors(value.model_dump()))
    if errors:
        raise ValueError('工作流依赖未实际调用或复用选择不符合契约：' + '; '.join(
            '.'.join(map(str, error.absolute_path)) + ': ' + error.message for error in errors[:4]))
