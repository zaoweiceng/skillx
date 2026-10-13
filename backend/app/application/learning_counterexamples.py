"""A graph revision must keep exercising business inputs that already failed.

The Agent may repair verification programs. Input scenarios and structured
expected facts are not disposable merely because a different case passes.
"""
from app.application.compiler import digest_json
from app.application.workflow_assertions import check_expected


def capture_counterexample(example, error):
    text = str(error)
    # These show a broken verifier, not a demonstrated business counterexample.
    if any(kind in text for kind in ('NameError', 'SyntaxError', 'IndentationError',
        'UnboundLocalError', 'verify 必须包含', 'verify 不得打印', 'verify 隔离执行失败')):
        return None
    value = example.model_dump(mode='json')
    return {'id': digest_json({'input': value['input'], 'setup': value.get('setup', '')}),
            'example': value, 'failure': text}


def validate_counterexamples(examples, obligations):
    for obligation in obligations:
        original = obligation['example']
        candidates = [example for example in examples if example.input == original['input']
                      and example.setup == original.get('setup', '')]
        if not candidates:
            raise ValueError('修订丢失已失败的业务输入场景：' + obligation['id'] +
                             '；保留该 input/setup，修复实现或映射后实际复验，不能换用另一种输入绕过')
        matched = False
        for example in candidates:
            try:
                check_expected(original['expected'], example.expected, partial=True)
                matched = True
                break
            except ValueError:
                pass
        if not matched:
            raise ValueError('修订撤销已声明的反例业务事实：' + obligation['id'] +
                             '；允许修正 verify 程序，但不能删除已有 expected 事实')
