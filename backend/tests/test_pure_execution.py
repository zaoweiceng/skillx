from types import SimpleNamespace

import pytest

from app.application.learning_contracts import ContractExample, SkillXProposal
from app.application.task_learning import materialize_skill
from app.config import get_settings
from app.runtime.skillx_host import execute_in_subprocess


def proposal(source):
    return SkillXProposal(name='pure-boundary', description='Behavior-based pure execution',
        inputSchema={'type': 'object'}, outputSchema={'type': 'object'}, source=source,
        examples=[ContractExample(input={'x': 1}, expected={'x': 1}, sourceInvocationId='call'),
            ContractExample(input={'x': 2}, expected={'x': 2})])


async def build(source, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), 'capability_root', tmp_path / 'packages')
    return await materialize_skill(proposal(source), 'pure-test', {
        'call': SimpleNamespace(status='succeeded', kind='code', output_json={'stdout': '{"x":1}'})})


async def test_general_stdlib_computation_runs_in_validation_and_production(tmp_path, monkeypatch):
    root = await build('''import gzip, ipaddress, pathlib, io, os, urllib.request
from email.utils import parseaddr
from http import HTTPStatus

def execute(request, context):
    assert gzip.decompress(gzip.compress(b"hello")) == b"hello"
    assert str(ipaddress.ip_address("192.0.2.1")) == "192.0.2.1"
    assert str(pathlib.PurePosixPath("a") / "b") == "a/b"
    assert io.StringIO("text").read() == "text"
    assert parseaddr("User <user@example.test>")[1] == "user@example.test"
    assert int(HTTPStatus.OK) == 200
    return request.input
''', tmp_path, monkeypatch)
    result = await execute_in_subprocess(root, 'execute', 'published-computation', {'x': 9})
    assert result.status == 'succeeded', result.error
    assert result.output == {'x': 9}


@pytest.mark.parametrize('operation', [
    "open(request.input['path']).read()",
    "open(request.input['path'], 'w').write('modified')",
    "__import__('pathlib').Path(request.input['path']).read_text()",
    "__import__('subprocess').run(['python3', '-c', 'print(1)'])",
    "__import__('socket').create_connection(('127.0.0.1', 8000), timeout=1)",
])
async def test_published_pure_boundary_rejects_effects_not_seen_in_examples(tmp_path, monkeypatch, operation):
    # Import names are normal; the branch is deliberately absent from build examples.
    operation = operation.replace("__import__('pathlib')", 'pathlib').replace("__import__('subprocess')", 'subprocess').replace("__import__('socket')", 'socket')
    source = f'''import pathlib, subprocess, socket

def execute(request, context):
    if 'path' in request.input:
        {operation}
    return request.input
'''
    root = await build(source, tmp_path, monkeypatch)
    protected = tmp_path / 'unrelated-business.txt'
    protected.write_text('unchanged')
    result = await execute_in_subprocess(root, 'execute', 'published-effect', {'path': str(protected)})
    assert result.status == 'failed'
    assert 'pure-python' in result.error.message
    assert protected.read_text() == 'unchanged'


def test_os_boundary_blocks_business_read_even_without_python_audit(tmp_path):
    import subprocess
    import sys
    from app.runtime.pure_python import sandbox_command
    protected = tmp_path / 'business.txt'
    protected.write_text('private')
    result = subprocess.run(sandbox_command([sys.executable, '-c',
        'import sys; print(open(sys.argv[1]).read())', str(protected)]), capture_output=True, text=True)
    assert result.returncode != 0
    assert 'private' not in result.stdout
    assert 'PermissionError' in result.stderr


def test_legacy_generated_package_uses_verified_profile_without_artifact_rewrite(tmp_path):
    import json
    from app.runtime.pure_python import execution_profile
    (tmp_path / 'validation.json').write_text(json.dumps({'profile': 'pure-python', 'status': 'passed'}))
    assert execution_profile(tmp_path, {}) == 'pure-python'
    assert execution_profile(tmp_path, {'x-executionProfile': 'local-command'}) == 'local-command'
