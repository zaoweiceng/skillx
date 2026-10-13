import json
import sys
from types import SimpleNamespace

import pytest

from app.application.learning_contracts import ContractExample, SkillXProposal
from app.application.local_validation import LocalValidation
from app.application.task_learning import materialize_skill
from app.runtime.skillx import load_manifest

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox-exec validation')


async def test_fixture_stdout_contract_and_conflicts():
    workspace = LocalValidation()
    try:
        await workspace.setup('print(\'{"path":"repo","sha":"dynamic"}\')')
        assert await workspace.resolve_inputs({'sha': '${fixture.sha}'}) == {'sha': 'dynamic'}
        await workspace.setup("import pathlib\npathlib.Path('.skillx-fixture.json').write_text('{\"sha\":\"different\"}')\nprint('{\"sha\":\"dynamic\"}')")
        with pytest.raises(ValueError, match='输出通道冲突'):
            await workspace.resolve_inputs({'sha': '${fixture.sha}'})
    finally:
        workspace.close()


@pytest.mark.parametrize('url', ['https://example.invalid/team/repo.git', 'git@example.invalid:team/repo.git'])
async def test_isolated_git_transport_preserves_business_url(url):
    workspace = LocalValidation()
    try:
        await workspace.setup('''import pathlib, subprocess
bare = pathlib.Path('remote.git').resolve()
subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
subprocess.run(['git', 'config', '--global', 'url.' + str(bare) + '.insteadOf', ''' + repr(url) + '''], check=True)
''')
        result = await workspace.execute('''import subprocess
def execute(request, context):
    url = request.input['url']
    refs = subprocess.check_output(['git', 'ls-remote', url], text=True)
    return {'url': url, 'refs': refs}
''', {'url': url}, {'type': 'object'})
        assert result == {'url': url, 'refs': ''}
    finally:
        workspace.close()


async def test_command_profile_runs_real_git_with_independent_postconditions(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'capability_root', tmp_path)
    # Test fixture implementation only; product artifacts are authored by the Agent.
    proposal = SkillXProposal(
        name='test-create-branch', description='Create exactly one branch',
        executionProfile='local-command',
        inputSchema={'type': 'object', 'properties': {'branch': {'type': 'string'}}, 'required': ['branch']},
        outputSchema={'type': 'object', 'properties': {'branch': {'type': 'string'}}, 'required': ['branch']},
        source='''import subprocess

def execute(request, context):
    subprocess.run(['git', 'checkout', '-b', request.input['branch']], check=True, capture_output=True, timeout=10)
    return {'branch': request.input['branch']}
''',
        examples=[ContractExample(input={'branch': branch}, expected={}, sourceInvocationId='original',
            setup="import subprocess\nsubprocess.run(['git', 'init', '-q'], check=True)",
            verify=f"import subprocess\nassert subprocess.check_output(['git', 'symbolic-ref', '--short', 'HEAD'], text=True).strip() == {branch!r}\nassert result['branch'] == {branch!r}")
            for branch in ['feature-a', 'feature-b']],
    )
    package = await materialize_skill(proposal, 'test', {'original': SimpleNamespace(status='succeeded')})
    manifest = load_manifest(package)
    interface = manifest['spec']['interfaces']['execute']
    assert interface['effects'] == [{'type': 'local.command', 'confirmation': 'required'}]
    assert interface['execution']['idempotency'] == 'none'
    assert json.loads((package / 'validation.json').read_text())['examples'] == 2
    # A plausible return value is insufficient when the actual Git state is wrong.
    proposal.source = "def execute(request, context):\n    return {'branch': request.input['branch']}\n"
    with pytest.raises(ValueError, match='隔离命令验证失败'):
        await materialize_skill(proposal, 'broken', {'original': SimpleNamespace(status='succeeded')})


async def test_command_validation_cannot_write_outside_workspace(tmp_path):
    protected = tmp_path / 'original.txt'
    protected.write_text('unchanged')
    workspace = LocalValidation()
    try:
        with pytest.raises(ValueError, match='隔离命令验证失败'):
            await workspace.python(f"open({str(protected)!r}, 'w').write('modified')")
        assert protected.read_text() == 'unchanged'
        with pytest.raises(ValueError, match='隔离命令验证失败'):
            await workspace.python("import socket\nsocket.create_connection(('127.0.0.1', 8000), timeout=1)")
    finally:
        workspace.close()


async def test_validation_uses_production_request_and_context_objects():
    workspace = LocalValidation()
    try:
        result = await workspace.execute('''def execute(request, context):
    context.check_cancelled()
    assert request.interface == "execute"
    assert request.locale == "zh-CN"
    assert not isinstance(request, dict)
    return request.input
''', {"value": 3}, {"type": "object"})
        assert result == {"value": 3}
    finally:
        workspace.close()


async def test_normal_type_name_is_not_rejected_as_private_runtime_access(tmp_path, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), 'capability_root', tmp_path)
    proposal = SkillXProposal(name='type-name', description='Return input type name',
        inputSchema={'type': 'object'}, outputSchema={'type': 'object'},
        source='def execute(request, context): return {"type": type(request.input["value"]).__name__}',
        examples=[ContractExample(input={'value': 1}, expected={'type': 'int'}, sourceInvocationId='source'),
                  ContractExample(input={'value': 'x'}, expected={'type': 'str'})])
    root = await materialize_skill(proposal, 'type-test', {
        'source': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"type":"int"}'})})
    assert (root / 'execute.py').exists()


async def test_task_can_execute_code_with_empty_capability_catalog(monkeypatch, tmp_path):
    from uuid import uuid4
    from sqlalchemy import select
    from app.application import tasks
    from app.application.agent_host import AgentHost, AgentHostResult
    from app.application.invocations import InvocationDriver
    from app.config import get_settings
    from app.infrastructure.database import SessionLocal
    from app.infrastructure.models.tasks import Task, Invocation, LearningJob
    monkeypatch.setattr(get_settings(), 'artifact_root', tmp_path / 'artifacts')
    monkeypatch.setattr(get_settings(), 'task_local_execution_enabled', True)
    async def empty_catalog(session):
        return {'capabilities': [], 'tools': []}
    monkeypatch.setattr(tasks, 'task_catalog', empty_catalog)
    async def decide(self, request, **kwargs):
        assert '"commandLineViaCode":true' in request.prompt
        return AgentHostResult(output={
            'action': 'dispatch', 'summary': '直接读取 Git 版本', 'completionCriteria': ['读取版本'],
            'answer': '', 'calls': [{'key': 'git-version', 'kind': 'code', 'reason': '本机命令',
                'arguments': {'language': 'python', 'source': "import subprocess,json\nprint(json.dumps({'version':subprocess.check_output(['git','--version'],text=True).strip()}))"}}],
        })
    monkeypatch.setattr(AgentHost, 'execute', decide)
    task_id = str(uuid4())
    async with SessionLocal() as session, session.begin():
        session.add(Task(id=task_id, goal='读取 Git 版本', status='queued', state_json={}, runtime_json={}))
    await tasks.TaskDriver().poll_once()
    await InvocationDriver().poll_once()
    async with SessionLocal() as session:
        invocation = await session.scalar(select(Invocation).where(Invocation.task_id == task_id))
        assert invocation is not None and invocation.status == 'succeeded'
        assert 'git version' in invocation.output_json['stdout']
        assert await session.scalar(select(LearningJob.id)) is None


async def test_dynamic_fixture_input_is_resolved_without_eval():
    from app.application.local_validation import LocalValidation
    w = LocalValidation()
    try:
        await w.setup("import pathlib,json\npathlib.Path('.skillx-fixture.json').write_text(json.dumps({'sha':'fresh-sha','paths':['chosen']}))")
        assert await w.resolve_inputs({'sha':'${fixture.sha}','nested':['${fixture.paths}'],'literal':'HEAD'}) == {'sha':'fresh-sha','nested':[['chosen']],'literal':'HEAD'}
        with pytest.raises(ValueError, match='缺少动态值'):
            await w.resolve_inputs({'sha':'${fixture.missing}'})
    finally:
        w.close()


async def test_expected_rejection_requires_matching_error_and_preserves_state():
    from app.application.learning_contracts import SkillXExample
    example = SkillXExample(input={}, expected={}, setup="from pathlib import Path\nPath('index').write_text('unchanged')",
        expectedError={'type': 'ValueError', 'messageContains': 'foreign staged files'},
        verify="from pathlib import Path\nassert error['type'] == 'ValueError'\nassert Path('index').read_text() == 'unchanged'")
    workspace = LocalValidation()
    try:
        await workspace.setup(example.setup)
        result = await workspace.execute("def execute(request, context):\n    raise ValueError('foreign staged files')", {}, {}, example.expectedError)
        await workspace.verify(example, result)
        for source in ["def execute(request, context): return {}", "def execute(request, context): raise TypeError('foreign staged files')"]:
            with pytest.raises(ValueError, match='未发生或错误不匹配'):
                await workspace.execute(source, {}, {}, example.expectedError)
    finally:
        workspace.close()

async def test_local_verify_receives_resolved_inputs_and_expected_for_each_example():
    from app.application.learning_contracts import SkillXExample
    for branch in ['feature/x', 'fix/y']:
        example = SkillXExample(input={'branch': '${fixture.branch}'}, expected={'valid': True},
            setup="import json,pathlib\npathlib.Path('.skillx-fixture.json').write_text(json.dumps({'branch': " + repr(branch) + "}))",
            verify="assert result['branch'] == inputs['branch']\nassert expected['valid'] is True\nassert error is None")
        workspace = LocalValidation()
        try:
            await workspace.setup(example.setup)
            inputs = await workspace.resolve_inputs(example.input)
            await workspace.verify(example, {'branch': branch, 'valid': True}, inputs)
            with pytest.raises(ValueError, match='隔离命令验证失败：AssertionError'):
                await workspace.verify(example, {'branch': 'wrong', 'valid': True}, inputs)
            example.verify = 'assert missing_variable'
            with pytest.raises(ValueError) as error:
                await workspace.verify(example, {'branch': branch, 'valid': True}, inputs)
            assert "NameError: name 'missing_variable' is not defined" in str(error.value).splitlines()[0]
        finally:
            workspace.close()
