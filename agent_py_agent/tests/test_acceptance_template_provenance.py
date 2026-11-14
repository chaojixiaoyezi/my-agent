"""模板派生保存原成员引用；出处只供追溯，不授予执行或包写权限。"""
import copy
import hashlib
import json

import pytest

from agent_py_agent.agent.assets.workflow_package_template import WorkflowTemplateError
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.business_spaces import v13_w7_playbooks as playbooks
from agent_py_agent.agent.capability.package_resources import package_resource_reference
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_call_frozen
from agent_py_agent.tests import test_v13_w7_playbooks as fixtures
from agent_py_agent.tests.test_v13_w7_playbooks import _call, _install_content

v13_host = fixtures.v13_host


def test_package_origin_survives_public_and_store_edits(v13_host):
    call = _call(v13_host, 'workflows.templates.list', {'tag': 'recovery'})
    entry = _install_content(call)
    package = next(p for p in playbooks._packages(call) if p.package_id == entry.manifest.plugin_id)
    member = 'workflows/recovery.json'
    original = package.read(member)
    template = playbooks.list_templates(call)['items'][0]
    expected = package_resource_reference(package, member)
    assert template['definition']['descriptor']['metadata']['derived_from'] == expected
    assert expected['resource_sha256'] == hashlib.sha256(original).hexdigest()
    assert expected['content_sha256'] == entry.package_sha256
    created = v13_call_frozen(call, 'workflows.create', {'definition': template['definition']},
                              idempotency_key='derive-template')
    store = WorkflowStore(call.owner_home)
    before = store.get_workflow(created['workflow_ref'])
    edited = copy.deepcopy(template['definition'])
    edited['descriptor'].pop('metadata')
    edited['body_markdown'] = '编辑副本，不回写包'
    v13_call_frozen(call, 'workflows.update', {'workflow_ref': created['workflow_ref'], 'definition': edited},
                    idempotency_key='edit-without-origin', expected_revision=before.descriptor.revision)
    saved = store.get_workflow(created['workflow_ref'])
    assert saved.descriptor.metadata['derived_from'] == expected
    assert saved.body_markdown == edited['body_markdown']
    # 普通存储入口也不能用正文 metadata 擦除或重定向已经记录的出处。
    value = saved.to_dict()
    value['descriptor']['metadata']['derived_from'] = {'package_id': 'not-the-original'}
    value['body_markdown'] = '第二次编辑'
    with pytest.raises(WorkflowTemplateError, match='WORKFLOW_TEMPLATE_SOURCE_HOST_ONLY'):
        store.save_workflow(value, expected_revision=saved.descriptor.revision)
    rejected = WorkflowStore(call.owner_home).get_workflow(created['workflow_ref'])
    assert rejected.to_dict() == saved.to_dict()
    value['descriptor']['metadata']['derived_from'] = copy.deepcopy(expected)
    store.save_workflow(value, expected_revision=saved.descriptor.revision)
    reopened = WorkflowStore(call.owner_home).get_workflow(created['workflow_ref'])
    assert reopened.descriptor.metadata['derived_from'] == expected
    assert reopened.descriptor.source_scope == 'owner'
    assert reopened.body_markdown == '第二次编辑'
    assert reopened.descriptor.revision == saved.descriptor.revision + 1
    assert package.read(member) == original
    assert 'derived_from' not in json.loads(original)['descriptor'].get('metadata', {})


def test_template_listing_does_not_mutate_member_or_share_metadata(v13_host):
    call = _call(v13_host, 'workflows.templates.list', {'tag': 'recovery'})
    _install_content(call)
    first = playbooks.list_templates(call)
    original = copy.deepcopy(first['items'][0]['definition'])
    first['items'][0]['definition']['descriptor']['metadata']['derived_from']['resource_path'] = 'changed.json'
    assert playbooks.list_templates(call)['items'][0]['definition'] == original
