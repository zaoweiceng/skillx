from copy import deepcopy

from app.application.learning_context import extraction_call_preview


def test_planner_uses_canonical_json_and_preserves_complete_builder_evidence():
    record = {'id': 'created', 'method': 'POST', 'body': {'title': 'A'}, 'status': 201}
    call = {'id': 'one', 'kind': 'code', 'status': 'succeeded', 'input': {'source': 'actual source'},
            'output': {'stdout': 'raw duplicate', 'argv': ['private path']}, 'observedOutput': record}
    before = deepcopy(call)
    preview = extraction_call_preview(call)
    assert preview['observedOutput'] == record
    assert 'output' not in preview
    assert call == before


def test_process_metadata_cannot_crowd_out_protocol_evidence_or_failure():
    call = {'id': 'two', 'kind': 'code', 'status': 'failed', 'input': {},
            'observedOutput': {'argv': ['x' * 3000], 'requestDigest': 'digest', 'exitCode': 1,
                               'stdout': 'HTTP 503', 'stderr': 'read failed', 'error': 'failure'},
            'error': {'code': 'FAILED'}}
    preview = extraction_call_preview(call)
    assert preview['status'] == 'failed' and preview['error'] == {'code': 'FAILED'}
    assert preview['observedOutput'] == {'stdout': 'HTTP 503', 'stderr': 'read failed', 'exitCode': 1, 'error': 'failure'}


def test_large_protocol_preview_is_explicit_not_relabelled_complete():
    call = {'id': 'three', 'kind': 'code', 'status': 'succeeded', 'input': {},
            'observedOutput': {'actual': 'x' * 5000}}
    preview = extraction_call_preview(call)['observedOutput']
    assert preview['truncated'] is True
    assert 'instruction' not in preview
    assert call['observedOutput']['actual'] == 'x' * 5000
