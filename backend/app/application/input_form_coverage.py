"""Coverage for Agent-declared alternative input forms, using real executed inputs."""
import json
from jsonschema import Draft202012Validator


def declared_forms(schema, path=()):
    if not isinstance(schema, dict):
        return
    for keyword in ('oneOf', 'anyOf'):
        branches = schema.get(keyword, [])
        if len(branches) > 1 and any(isinstance(branch, dict) and branch.get('title') for branch in branches):
            if not all(isinstance(branch, dict) and isinstance(branch.get('title'), str) and branch['title'].strip() for branch in branches):
                raise ValueError('声明多种输入形式时每个分支都必须提供非空 title：input.' + '.'.join(path))
            yield path, keyword, branches
    for name, child in schema.get('properties', {}).items():
        yield from declared_forms(child, (*path, name))


def validate_input_form_coverage(schema, successful_inputs):
    for path, keyword, branches in declared_forms(schema):
        seen = set()
        for inputs in successful_inputs:
            value = inputs
            for name in path:
                if not isinstance(value, dict) or name not in value:
                    break
                value = value[name]
            else:
                matches = [index for index, branch in enumerate(branches)
                           if Draft202012Validator(branch).is_valid(value)]
                # Labels alone cannot make identical examples count as different forms.
                if len(matches) == 1:
                    seen.add(matches[0])
        missing = [branch['title'] for index, branch in enumerate(branches) if index not in seen]
        if missing:
            raise ValueError('声明输入形式缺少可区分的成功执行样例：input.' + '.'.join(path)
                             + '.' + keyword + ' → ' + '、'.join(missing)
                             + '；请覆盖这些形式，不能用同一种形式的重复样例或仅 title 的空约束冒充。')


def check_declared_scope_change(previous, revised, reason, old_metadata, new_metadata):
    def signatures(schema):
        return {(path, keyword, branch['title'], json.dumps(branch, sort_keys=True))
                for path, keyword, branches in declared_forms(schema) for branch in branches}
    changed = signatures(previous) - signatures(revised)
    if changed and (not reason or not reason.strip()):
        raise ValueError('修订删除或改变已声明输入形式，必须用 scopeChange 明确说明范围变化，不能静默规避覆盖验证')
    if changed and old_metadata == new_metadata:
        raise ValueError('范围变化必须同步工作流说明，不能保留未覆盖形式的原有支持承诺')
