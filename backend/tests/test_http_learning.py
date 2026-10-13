import json
import sys
from types import SimpleNamespace
from urllib.request import Request

import pytest

from app.application.learning_contracts import ContractExample, HttpFixture
from app.application.local_validation import LocalValidation
from app.application.extraction_effects import validate_required_effects
from app.runtime.http_validation import HttpFixtures

FIXTURE = {"method": "POST", "url": "https://fixture.invalid/notes", "requestJson": {"title": "A"}, "responseJson": {"id": "n1"}, "status": 201}
SOURCE = '''import json
from urllib.request import Request, urlopen
def execute(request, context):
    with urlopen(Request(request.input['url'], data=json.dumps({'title': request.input['title']}).encode(), method='POST'), timeout=5) as response:
        return json.load(response)
'''


def test_fixture_exact_method_url_body_and_response():
    fixtures = HttpFixtures([FIXTURE])
    with fixtures.urlopen(Request(FIXTURE['url'], data=b'{"title":"A"}', method='POST')) as response:
        assert response.status == 201
        assert json.load(response) == {'id': 'n1'}
    fixtures.assert_consumed()
    assert not fixtures.violations


@pytest.mark.parametrize('method,url,body', [('GET', FIXTURE['url'], {'title': 'A'}), ('POST', 'https://fixture.invalid/other', {'title': 'A'}), ('POST', FIXTURE['url'], {'title': 'wrong'})])
def test_request_mismatch_persists_violation(method, url, body):
    fixtures = HttpFixtures([FIXTURE])
    with pytest.raises(AssertionError, match='mismatch'):
        fixtures.urlopen(Request(url, data=json.dumps(body).encode(), method=method))
    assert fixtures.consumed == 0
    assert fixtures.violations


def test_required_effect_rejects_pure_formatter_and_missing_write_fixture():
    proposal = SimpleNamespace(kind='direct', executionProfile='pure-python', examples=[])
    validate_required_effects([], proposal)
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_required_effects(['http.write'], proposal)
    proposal.executionProfile = 'local-command'
    proposal.examples = [ContractExample(input={}, expected={}, httpFixtures=[{**FIXTURE, 'method': 'GET'}])]
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_required_effects(['http.write'], proposal)


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_real_http_source_consumes_fixture_and_formatter_fails():
    workspace = LocalValidation()
    example = ContractExample(input={'url': FIXTURE['url'], 'title': 'A'}, expected={'id': 'n1'}, setup='pass', verify="assert result['id'] == 'n1'", httpFixtures=[FIXTURE])
    try:
        await workspace.setup(example.setup)
        await workspace.configure_http(example)
        result = await workspace.execute(SOURCE, example.input, {'type': 'object'})
        await workspace.verify(example, result)
        await workspace.configure_http(example)
        result = await workspace.execute("def execute(request, context): return {'id':'n1'}", example.input, {'type': 'object'})
        with pytest.raises(ValueError, match='not consumed'):
            await workspace.verify(example, result)
    finally:
        workspace.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_swallowed_extra_request_still_fails_and_raw_network_blocked():
    workspace = LocalValidation()
    try:
        example = ContractExample(input={'url': FIXTURE['url'], 'title': 'A'}, expected={}, httpFixtures=[FIXTURE])
        await workspace.configure_http(example)
        source = SOURCE.replace('return json.load(response)', '''result = json.load(response)
    try:
        urlopen('https://fixture.invalid/extra')
    except Exception:
        pass
    return result''')
        with pytest.raises(ValueError, match='HTTP fixture violation'):
            await workspace.execute(source, example.input, {'type': 'object'})
        with pytest.raises(ValueError, match='候选执行失败'):
            await workspace.execute("import socket\ndef execute(request, context):\n    socket.create_connection(('127.0.0.1', 3210), timeout=1)\n    return {}", {}, {'type': 'object'})
    finally:
        workspace.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_undeclared_write_cannot_pass_as_expected_business_error():
    from app.application.learning_contracts import ExpectedExecutionError
    workspace = LocalValidation()
    try:
        source = '''import urllib.request
def execute(request, context):
    urllib.request.urlopen(urllib.request.Request('https://fixture.invalid/write', data=b'{}', method='POST'))
'''
        with pytest.raises(ValueError, match='HTTP fixture violation'):
            await workspace.execute(source, {}, {'type': 'object'},
                ExpectedExecutionError(type='AssertionError', messageContains='HTTP fixture missing'))
    finally:
        workspace.close()


def test_production_source_executes_real_http_without_validation_adapter():
    """A disposable in-test server, never the user's application or records."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b'{"id":"actual-server-id"}')
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        namespace = {}
        exec(SOURCE, namespace)
        result = namespace['execute'](SimpleNamespace(input={'url': f'http://127.0.0.1:{server.server_port}/notes', 'title': 'Actual'}), None)
        assert result == {'id': 'actual-server-id'}
        assert seen == [('/notes', {'title': 'Actual'})]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_workflow_nodes_share_single_ordered_http_queue():
    workspace = LocalValidation()
    second = {'method': 'GET', 'url': 'https://fixture.invalid/notes/n1', 'responseJson': {'id': 'n1', 'title': 'A'}}
    example = ContractExample(input={'url': FIXTURE['url'], 'title': 'A'}, expected={'id': 'n1', 'title': 'A'}, setup='pass', verify="assert result['title'] == inputs['title']", httpFixtures=[FIXTURE, second])
    try:
        await workspace.setup(example.setup)
        await workspace.configure_http(example)
        created = await workspace.execute(SOURCE, example.input, {'type': 'object'})
        assert len(workspace.http_fixtures) == 1
        readback = await workspace.execute('''import json
from urllib.request import urlopen
def execute(request, context):
    with urlopen(request.input['url']) as response:
        return json.load(response)
''', {'url': FIXTURE['url'] + '/' + created['id']}, {'type': 'object'})
        await workspace.verify(example, readback, example.input)
    finally:
        workspace.close()


def test_direct_reuse_cannot_claim_external_effect_from_pure_formatter():
    from app.application.learning_agents import ExtractionPlan
    from app.application.extraction_reuse import validate_reuse_plan
    plan = ExtractionPlan(decision='candidate', reason='reuse', skillx=[{'name':'create-note', 'description':'create', 'scope':'HTTP create', 'useWhen':['create'], 'sourceInvocationIds':['source'], 'reuseVersionId':'pure', 'requiredEffects':['http.write']}])
    context = {'catalog': {'capabilities': [{'id':'pure', 'interfaces': {'execute': {'executionProfile':'pure-python', 'effects':[], 'hasDependencies':False}}}]}}
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_reuse_plan(plan, context)
    plan.skillx[0].requiredEffects = []
    validate_reuse_plan(plan, context)


def test_workflow_effects_covered_across_readonly_reuse_and_create_branches():
    from app.application.extraction_effects import validate_http_examples
    read = {'method': 'GET', 'url': FIXTURE['url'], 'responseJson': []}
    examples = [
        ContractExample(input={'title':'exists'}, expected={}, httpFixtures=[read]),
        ContractExample(input={'title':'new'}, expected={}, httpFixtures=[read, FIXTURE]),
    ]
    validate_http_examples(['http.read', 'http.write'], examples, across_examples=True)
    # Atomic operations retain per-success-example obligations.
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_http_examples(['http.read', 'http.write'], examples)


def test_workflow_readonly_samples_cannot_prove_write_effect():
    from app.application.extraction_effects import validate_http_examples
    read = {'method': 'GET', 'url': FIXTURE['url'], 'responseJson': []}
    examples = [ContractExample(input={}, expected={}, httpFixtures=[read])]
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_http_examples(['http.read', 'http.write'], examples, across_examples=True)


def test_http_failure_coverage_requires_business_error_not_just_status():
    from app.application.extraction_effects import validate_http_failure_coverage
    from app.application.learning_contracts import SkillXExample
    happy = SkillXExample(input={}, expected={}, httpFixtures=[FIXTURE])
    with pytest.raises(ValueError, match='4xx/5xx'):
        validate_http_failure_coverage([happy])
    failure = SkillXExample(input={}, expected={}, httpFixtures=[{**FIXTURE, 'status':503}], failureOutput={'status':503})
    with pytest.raises(ValueError, match='仅状态码'):
        validate_http_failure_coverage([happy, failure])
    failure.failureOutput = {'status':'503'}
    with pytest.raises(ValueError, match='仅状态码'):
        validate_http_failure_coverage([happy, failure])
    failure.failureOutput = {'matched':False}
    validate_http_failure_coverage([happy, failure])
    failure.failureOutput = None
    from app.application.learning_contracts import ExpectedExecutionError
    failure.expectedError = ExpectedExecutionError(type='HTTPError', messageContains='503')
    validate_http_failure_coverage([happy, failure])


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_http_failure_output_cannot_claim_success_and_transcript_is_host_supplied():
    from app.application.learning_contracts import SkillXExample
    workspace = LocalValidation()
    failure = SkillXExample(input={}, expected={}, setup='pass', verify="assert httpRequests[0]['status'] == 503", httpFixtures=[{'method':'GET','url':'https://fixture.invalid/item','status':503,'responseJson':{'id':'1'}}], failureOutput={'matched':False})
    source = '''from urllib.request import urlopen
from urllib.error import HTTPError
def execute(request, context):
    try:
        urlopen('https://fixture.invalid/item')
    except HTTPError:
        return {'matched':True, 'status':503}
'''
    try:
        await workspace.configure_http(failure)
        result = await workspace.execute(source, {}, {'type':'object'})
        with pytest.raises(ValueError):
            await workspace.verify(failure, result)
        await workspace.configure_http(failure)
        result = await workspace.execute(source.replace("'matched':True", "'matched':False"), {}, {'type':'object'})
        await workspace.verify(failure, result)
    finally:
        workspace.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='OS sandbox validation')
async def test_sent_yaml_roundtrip_uses_actual_host_request_not_candidate_output():
    import yaml
    fields = {'title':'A: B\n"quoted"', 'summary':'true', 'tags':[]}
    text = '\n'.join(key + ': ' + json.dumps(value) for key, value in fields.items())
    workspace = LocalValidation()
    fixture = {**FIXTURE, 'requestJson':{'text':text}}
    source = '''import json
from urllib.request import Request, urlopen
def execute(request, context):
    text = '\\n'.join(key + ': ' + json.dumps(value) for key, value in request.input.items())
    with urlopen(Request('https://fixture.invalid/notes', data=json.dumps({'text':text}).encode(), method='POST')) as response:
        return {'ok':True}
'''
    example = ContractExample(input=fields, expected={'ok':True}, setup='pass', verify="import yaml\nassert yaml.safe_load(httpRequests[0]['requestJson']['text']) == inputs", httpFixtures=[fixture])
    try:
        await workspace.configure_http(example)
        result = await workspace.execute(source, fields, {'type':'object'})
        await workspace.verify(example, result, fields)
        # A matching transport fixture alone cannot prove correct serialization.
        bad_text = 'title: A: B\nsummary: true\ntags:'
        example.httpFixtures[0].requestJson = {'text':bad_text}
        await workspace.configure_http(example)
        broken = source.replace("text = '\\n'.join(key + ': ' + json.dumps(value) for key, value in request.input.items())", 'text = ' + repr(bad_text))
        result = await workspace.execute(broken, fields, {'type':'object'})
        with pytest.raises(ValueError, match='ScannerError'):
            await workspace.verify(example, result, fields)
    finally:
        workspace.close()


def test_failure_output_cannot_count_as_success_effect_coverage_or_only_success():
    from app.application.learning_contracts import SkillXExample, SkillXProposal
    from app.application.extraction_effects import validate_http_examples
    failure = SkillXExample(input={}, expected={}, httpFixtures=[{**FIXTURE,'status':503}], failureOutput={'matched':False})
    with pytest.raises(ValueError, match='requiredEffects'):
        validate_http_examples(['http.write'], [failure], across_examples=True)
    with pytest.raises(ValueError, match='至少一个样例'):
        SkillXProposal(name='only-errors', description='must not publish', executionProfile='local-command', inputSchema={'type':'object'}, outputSchema={'type':'object'}, source='def execute(request, context): return {}', examples=[failure,failure])
