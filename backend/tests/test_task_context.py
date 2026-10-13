import json
from copy import deepcopy
from pathlib import Path

from app.application.task_context import call_context, catalog_context


def record(index):
    return {
        "id": f"id-{index}", "key": f"call-{index}", "kind": "code", "status": "succeeded",
        "request": {"language": "python", "source": "print('large script')\n" * 1000},
        "output": {"stdout": f"result-{index}:" + "a" * 10000}, "error": None,
    }


def test_history_is_bounded_and_exact_evidence_can_be_read_without_reexecution(tmp_path):
    records = [record(index) for index in range(160)]
    original = deepcopy(records)
    calls, history = call_context(records, tmp_path)
    assert len(json.dumps(calls, ensure_ascii=False, separators=(",", ":"))) <= 24000
    assert len(calls) < len(records)
    assert history["omittedCalls"] == len(records) - len(calls)
    assert calls[-1]["id"] == "id-159"
    assert [call["id"] for call in calls] == [r["id"] for r in records[-len(calls):]]
    assert "source" not in calls[-1]["request"]
    assert calls[-1]["output"]["stdout"]["truncated"]
    assert json.loads(Path(calls[-1]["recordPath"]).read_text()) == original[-1]
    assert json.loads(Path(history["path"]).read_text()) == original
    assert records == original
    assert call_context(records, tmp_path) == (calls, history)


def test_small_structured_results_remain_directly_usable(tmp_path):
    item = record(1)
    item.update(kind="capability", request={"capabilityVersionId": "v1", "input": {}},
                output={"message": "你好"})
    calls, history = call_context([item], tmp_path)
    assert calls[0]["request"] == item["request"]
    assert calls[0]["output"] == {"message": "你好"}
    assert calls[0]["id"] == item["id"]
    assert history["omittedCalls"] == 0


def test_catalog_preserves_selection_metadata_and_exact_contracts(tmp_path):
    contract = {"id": "v1", "name": "weather", "description": "Current weather",
                "useWhen": ["weather question"], "doNotUseWhen": ["forecast"],
                "interfaces": {"execute": {"inputSchema": {"type": "object",
                "properties": {"city": {"type": "string"}}, "required": ["city"]}}}}
    catalog = {"capabilities": [contract], "tools": [{"name": "read.local"}]}
    original = deepcopy(catalog)
    result = catalog_context(catalog, tmp_path)
    entry = result["capabilities"][0]
    assert entry["id"] == "v1"
    assert entry["interfaces"] == ["execute"]
    assert entry["useWhen"] == contract["useWhen"]
    assert entry["doNotUseWhen"] == contract["doNotUseWhen"]
    assert json.loads(Path(entry["contractPath"]).read_text()) == contract
    assert result["tools"] == catalog["tools"]
    assert catalog == original


def test_small_document_is_returned_whole_despite_large_audit_metadata(tmp_path):
    item = record(1)
    document = ''.join(f'{i}: 真实条款与证据。\n' for i in range(1000))
    item['output'] = {'argv': ['long/private/path/' * 200], 'requestDigest': 'x' * 64,
                      'stdout': document, 'stderr': '', 'exitCode': 0}
    calls, _ = call_context([item], tmp_path)
    assert calls[0]['output']['stdout'] == document
    assert 'argv' not in calls[0]['output']
    assert json.loads(Path(calls[0]['recordPath']).read_text()) == item


def test_all_eight_parallel_read_results_remain_visible(tmp_path):
    records = [record(i) for i in range(8)]
    for i, item in enumerate(records):
        item['output'] = {'argv': ['x' * 1000], 'requestDigest': 'x' * 64,
                          'stdout': f'FILE-{i}\n' + '条款' * 700, 'exitCode': 0}
    calls, history = call_context(records, tmp_path)
    assert history['omittedCalls'] == 0
    assert [x['output']['stdout'] for x in calls] == [r['output']['stdout'] for r in records]


def test_large_results_share_budget_and_keep_business_content(tmp_path):
    records = [record(i) for i in range(8)]
    for item in records:
        item['output'] = {'argv': ['x' * 3000], 'stdout': 'evidence-"\\\n' * 5000,
                          'stderr': '', 'exitCode': 0}
    calls, _ = call_context(records, tmp_path)
    assert len(calls) == 8
    assert len(json.dumps(calls, ensure_ascii=False, separators=(',', ':'))) <= 24000
    for item in calls:
        assert item['output']['stdout']['preview'].startswith('evidence-')
        assert item['output']['stdout']['truncated']
        assert item['output']['exitCode'] == 0


def test_discovery_catalog_omits_contracts_but_keeps_exact_lazy_access(tmp_path):
    catalog = {'capabilities': [{'id': 'v1', 'name': 'convert', 'description': 'Convert data',
                                'interfaces': {'execute': {'inputSchema': {'type': 'object'}}}}],
               'tools': [{'capabilityRef': 'read.local', 'inputSchema': {'type': 'object'}}]}
    result = catalog_context(catalog, tmp_path, discovery_only=True)
    assert 'inputSchema' not in json.dumps(result)
    assert result['capabilities'][0]['description'] == 'Convert data'
    assert result['tools'][0]['capabilityRef'] == 'read.local'
    assert json.loads(Path(result['catalogPath']).read_text()) == catalog


def test_multiple_reference_documents_do_not_erase_each_other(tmp_path):
    from app.config import get_settings

    records = [record(i) for i in range(3)]
    for i, item in enumerate(records):
        item['output'] = {'stdout': f'document-{i}\n' + '合同正文\n' * 3300,
                          'stderr': '', 'exitCode': 0}
    limit = get_settings().task_context_max_characters
    calls, history = call_context(records, tmp_path, limit=limit)
    assert history['omittedCalls'] == 0
    assert [x['output']['stdout'] for x in calls] == [r['output']['stdout'] for r in records]
    assert len(json.dumps(calls, ensure_ascii=False, separators=(',', ':'))) <= limit
