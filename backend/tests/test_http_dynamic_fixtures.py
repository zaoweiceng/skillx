"""Execution-time expected data is derived independently; matching stays exact."""
import json
import sys
from urllib.request import Request

import pytest

from app.application.learning_contracts import ContractExample
from app.application.local_validation import LocalValidation
from app.runtime.http_validation import HttpFixtures

URL = 'https://fixture.invalid/items'
FIXTURE = {'method': 'POST', 'url': URL, 'requestJson': {'resourceId': None, 'title': 'A'},
           'responseJson': {'resourceId': None}, 'status': 201,
           'prepare': "from pathlib import Path\nassert not {'actual', 'request', 'candidate', 'module'} & globals().keys()\nvalue = Path('runtime-id.txt').read_text().strip()\nrequestJson['resourceId'] = value\nresponseJson['resourceId'] = value"}


@pytest.mark.parametrize('body', [
    {'resourceId': 'actual-id', 'title': 'A'},
    {'resourceId': 'wrong-id', 'title': 'A'},
    {'resourceId': 'actual-id', 'title': 'A', 'extra': True},
])
def test_dynamic_expected_body_remains_exact(tmp_path, monkeypatch, body):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'runtime-id.txt').write_text('actual-id')
    http = HttpFixtures([FIXTURE], allow_prepare=True)
    request = Request(URL, data=json.dumps(body).encode(), method='POST')
    if body == {'resourceId': 'actual-id', 'title': 'A'}:
        assert json.load(http.urlopen(request)) == {'resourceId': 'actual-id'}
        http.assert_consumed()
        assert http.requests[0]['requestJson'] == body
    else:
        with pytest.raises(AssertionError, match='mismatch'):
            http.urlopen(request)
        assert http.consumed == 0
        assert http.violations


@pytest.mark.parametrize('script', ["raise ValueError('independent failure')", 'import time; time.sleep(6)', "responseJson = 'x' * 1048577"])
def test_preparation_failure_never_consumes_or_accepts_request(script):
    http = HttpFixtures([{**FIXTURE, 'prepare': script}], allow_prepare=True)
    with pytest.raises(ValueError, match='prepare'):
        http.urlopen(Request(URL, data=b'{}', method='POST'))
    assert http.consumed == 0
    assert http.violations


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_runtime_value_from_previous_node_and_response_echo_are_independent():
    workspace = LocalValidation()
    example = ContractExample(input={}, expected={}, setup='pass',
        verify="from pathlib import Path\nassert result['resourceId'] == Path('runtime-id.txt').read_text()\nassert httpRequests[0]['requestJson'] == {'resourceId': result['resourceId'], 'title': 'A'}", httpFixtures=[FIXTURE])
    try:
        await workspace.setup(example.setup)
        await workspace.configure_http(example)
        generated = await workspace.execute("from pathlib import Path\nfrom uuid import uuid4\ndef execute(request, context):\n    value = str(uuid4())\n    Path('runtime-id.txt').write_text(value)\n    return {'resourceId': value}", {}, {'type': 'object'})
        source = "import json\nfrom urllib.request import Request, urlopen\ndef execute(request, context):\n    body = {'resourceId': request.input['resourceId'], 'title': 'A'}\n    return json.load(urlopen(Request('https://fixture.invalid/items', data=json.dumps(body).encode(), method='POST')))"
        result = await workspace.execute(source, generated, {'type': 'object'})
        await workspace.verify(example, result)
        example.httpFixtures[0].prepare = "import socket\nsocket.create_connection(('127.0.0.1', 3210), timeout=1)"
        await workspace.configure_http(example)
        with pytest.raises(ValueError, match='prepare failed'):
            await workspace.execute(source, generated, {'type': 'object'})
    finally:
        workspace.close()


def test_workflow_examples_schema_and_roundtrip_keep_preparation():
    from jsonschema import Draft202012Validator
    from app.application.learning_agents import WorkflowExamples
    example = {'input': {}, 'expected': {}, 'httpFixtures': [FIXTURE]}
    value = {'workflowExamples': [example, example]}
    Draft202012Validator(WorkflowExamples.model_json_schema()).validate(value)
    roundtrip = WorkflowExamples.model_validate_json(WorkflowExamples.model_validate(value).model_dump_json())
    assert roundtrip.workflowExamples[0].httpFixtures[0].prepare == FIXTURE['prepare']


def test_each_request_observes_current_state_without_mutating_static_fixture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    http = HttpFixtures([FIXTURE, FIXTURE], allow_prepare=True)
    for value in ['first', 'second']:
        (tmp_path / 'runtime-id.txt').write_text(value)
        request = Request(URL, data=json.dumps({'resourceId': value, 'title': 'A'}).encode(), method='POST')
        assert json.load(http.urlopen(request)) == {'resourceId': value}
    assert FIXTURE['requestJson']['resourceId'] is None
    http.assert_consumed()


def test_unconfined_http_fixture_path_cannot_enable_preparation():
    http = HttpFixtures([FIXTURE])
    with pytest.raises(ValueError, match='OS-isolated'):
        http.urlopen(Request(URL, data=b'{}', method='POST'))
    assert http.violations
    assert http.consumed == 0


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_swallowed_preparation_failure_is_still_host_violation():
    workspace = LocalValidation()
    try:
        example = ContractExample(input={}, expected={}, httpFixtures=[{**FIXTURE, 'prepare': "raise ValueError('broken expectation')"}])
        await workspace.configure_http(example)
        source = "from urllib.request import Request, urlopen\ndef execute(request, context):\n    try: urlopen(Request('https://fixture.invalid/items', data=b'{}', method='POST'))\n    except Exception: pass\n    return {}"
        with pytest.raises(ValueError, match='HTTP fixture violation.*prepare failed'):
            await workspace.execute(source, {}, {'type': 'object'})
    finally:
        workspace.close()
