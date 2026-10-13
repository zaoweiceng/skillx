import json
from types import SimpleNamespace

import pytest

from app.application.dependency_validation_context import load_dependency_validation_context
from app.config import get_settings
from app.infrastructure.database import CapabilityVersion, WorkflowVersion
from app.runtime.skillx import package_digest


class Session:
    def __init__(self, values):
        self.values = values
    async def get(self, model, id):
        return self.values.get((model, id))


def node(id='call', version='pinned', digest='digest', interface='execute'):
    return {'id':id,'type':'capability_call','config':{'capabilityVersionId':version,'digest':digest,'interface':interface}}


def graph(*nodes):
    return {'spec':{'nodes':list(nodes)}}


def example(**changes):
    return {'input':{'payload':'${fixture.payload}'}, 'expected':{'id':'n1'},
        'setup':"import json\nprint(json.dumps({'payload':{'text':'serialized protocol'}}))",
        'verify':"assert result['id'] == 'n1'",
        'httpFixtures':[{'method':'POST','url':'https://fixture.invalid/records','requestJson':'${fixture.payload}','responseJson':{'id':'n1'},'status':201}],
        'sourceInvocationId':'irrelevant-source-id', **changes}


def package(tmp_path, monkeypatch, examples=None):
    monkeypatch.setattr(get_settings(),'capability_root',tmp_path)
    folder=tmp_path/'pinned'
    folder.mkdir()
    (folder/'execute.py').write_text('DO NOT EXPOSE IMPLEMENTATION')
    (folder/'tests.json').write_text(json.dumps(examples if examples is not None else [example(),example(failureOutput={'success':False})]))
    digest=package_digest(folder)
    value=SimpleNamespace(id='pinned',status='published',package_path='pinned',digest=digest,
        manifest_json={'manifest':{'metadata':{'name':'existing-write'},'spec':{'defaultInterface':'execute','interfaces':{'execute':{'x-executionProfile':'local-command'},'other':{}}}},
                       'interfaces':{'execute':{'inputSchema':{'type':'object'},'outputSchema':{'type':'object'}}}})
    return folder,value


async def test_exact_pinned_tests_preserve_fixture_semantics_and_exclude_source(tmp_path,monkeypatch):
    folder,version=package(tmp_path,monkeypatch)
    session=Session({(CapabilityVersion,'pinned'):version})
    result=await load_dependency_validation_context(session,graph(node(digest=version.digest),node(id='second',digest=version.digest)))
    assert len(result['dependencies']) == 1
    item=result['dependencies'][0]
    assert item['nodePaths'] == ['call','second']
    assert item['versionId'] == 'pinned' and item['digest'] == version.digest
    expected={key:value for key,value in example().items() if key!='sourceInvocationId'}
    assert item['examples'] == [expected]
    assert item['failureExamples'] == [{**expected, 'failureOutput': {'success': False}}]
    assert item['examples'][0]['httpFixtures'][0]['requestJson'] == '${fixture.payload}'
    assert 'DO NOT EXPOSE' not in json.dumps(result)
    assert 'irrelevant-source-id' not in json.dumps(result)
    assert (folder/'execute.py').read_text() == 'DO NOT EXPOSE IMPLEMENTATION'


async def test_nested_pinned_workflow_and_generated_contracts(tmp_path,monkeypatch):
    _,version=package(tmp_path,monkeypatch)
    workflow=SimpleNamespace(digest='wf-digest',definition_json=graph(node(digest=version.digest)))
    session=Session({(CapabilityVersion,'pinned'):version,(WorkflowVersion,'wf'):workflow})
    definition=graph({'id':'nested','type':'workflow_call','config':{'workflowVersionId':'wf','digest':'wf-digest'}},
                     {'id':'new','type':'skillx','config':{'generatedSkill':'new-stage'}})
    result=await load_dependency_validation_context(session,definition,supplied_contracts=[{'name':'new-stage','executionProfile':'local-command','examples':[example()]}])
    assert result['dependencies'][0]['nodePaths'] == ['nested/call']
    assert result['dependencies'][1]['name'] == 'new-stage'
    assert result['dependencies'][1]['examples'][0]['setup'] == example()['setup']


async def test_digest_drift_and_wrong_interface_are_rejected(tmp_path,monkeypatch):
    folder,version=package(tmp_path,monkeypatch)
    session=Session({(CapabilityVersion,'pinned'):version})
    with pytest.raises(ValueError,match='digest'):
        await load_dependency_validation_context(session,graph(node(digest='wrong')))
    with pytest.raises(ValueError,match='接口不存在'):
        await load_dependency_validation_context(session,graph(node(digest=version.digest,interface='missing')))
    (folder/'tests.json').write_text('[]')
    with pytest.raises(ValueError,match='digest 漂移'):
        await load_dependency_validation_context(session,graph(node(digest=version.digest)))


async def test_does_not_apply_default_interface_tests_to_other_interface(tmp_path,monkeypatch):
    _,version=package(tmp_path,monkeypatch,[example(),example(interface='other',input={'other':1})])
    result=await load_dependency_validation_context(Session({(CapabilityVersion,'pinned'):version}),graph(node(digest=version.digest,interface='other')))
    assert result['dependencies'][0]['examples'][0]['input'] == {'other':1}
    assert len(result['dependencies'][0]['examples']) == 1


async def test_missing_samples_explicitly_reported_not_fabricated(tmp_path,monkeypatch):
    folder,version=package(tmp_path,monkeypatch,[])
    result=await load_dependency_validation_context(Session({(CapabilityVersion,'pinned'):version}),graph(node(digest=version.digest)))
    assert result['dependencies'][0]['examples'] == []
    assert result['dependencies'][0]['validationContractAvailable'] is False
