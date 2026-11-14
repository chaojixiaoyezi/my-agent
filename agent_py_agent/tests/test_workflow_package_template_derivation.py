"""真包成员沿既有资源读取、定义校验和Owner Store派生；来源不可由普通保存伪造。"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.capability.package_provider import enabled_capability_packages
from agent_py_agent.agent.capability.package_resources import (
    package_resource_reference,
    resolve_package_resource,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


def template():
    return {'descriptor': {'id': 'source-template', 'kind': 'workflow', 'name': '包内只读模板',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {'outputs': []}},
        'steps': [{'step_id': 'check', 'name': '纯条件', 'mode': 'condition',
            'config': {'expression': '1 == 1'}, 'on_error': 'fail'}]}


def installed(root, value, identity=None):
    owner = ensure_owner_home(root, identity)
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
    manifest = {'plugin_id': 'workflow-pack', 'version': '1.0.0', 'summary': '工作流模板测试包',
        'capability': {'description': '只读模板', 'keywords': ['workflow'], 'entry_document': 'README.md'},
        'files': [{'path': 'README.md'}, {'path': 'templates/check.json'}], 'settings_schema': {}}
    package = inspect_plugin_package(build_capability_pack(manifest,
        {'README.md': b'# workflow templates\n', 'templates/check.json': raw}).payload)
    store = PluginInstallStore(owner)
    entry = store.install(PluginInstallRequest(package, 'install-workflow-pack', 0)).installation
    activation = PluginContentActivation('activate-workflow-pack', 'workflow-pack', entry.package_sha256,
        entry.revision, entry.settings_revision)
    entry = store.change_activation(PluginActivationRequest(activation.operation_id, entry.revision, activation)).installation
    pack = next(pack for pack in enabled_capability_packages(owner) if pack.package_id == 'workflow-pack')
    snapshot = SkillSnapshot((), (), 'test', owner.owner_id, '', (pack,))
    agent = SimpleNamespace(current_skill_snapshot=lambda: snapshot)
    tool = WorkflowsTool(owner.home_dir, package_resource_resolver=lambda ref: resolve_package_resource(agent, ref))
    return SimpleNamespace(owner=owner, store=store, entry=entry, pack=pack, tool=tool,
        ref=package_resource_reference(pack, 'templates/check.json'), raw=raw)


def save(world, target='derived-check', **params):
    return world.tool.execute({'action': 'save', 'workflow_id': target, 'source_ref': world.ref, **params})


def test_actual_package_derives_to_independent_owner_without_changing_package(tmp_path):
    world = installed(tmp_path / 'home', template())
    before = world.store.package_bytes(world.entry)
    outcome = save(world)
    assert outcome.ok, outcome.output
    data = json.loads(outcome.output)
    source = data['descriptor']['metadata']['derived_from']
    assert source['verified'] is True and source['source_ref'] == world.ref
    assert source['source_ref']['resource_sha256'] == hashlib.sha256(world.raw).hexdigest()
    assert data['descriptor']['id'] == 'derived-check'
    assert world.store.package_bytes(world.entry) == before
    assert WorkflowStore(world.owner.home_dir).get_workflow('source-template') is None
    replay = save(world)
    assert not replay.ok and replay.effect_outcome == 'not_started'
    assert json.loads(replay.output)['error_code'] == 'WORKFLOW_TEMPLATE_TARGET_EXISTS'


@pytest.mark.parametrize('field', ['content_sha256', 'resource_sha256', 'activation_id'])
def test_wrong_digest_or_activation_never_writes_target(tmp_path, field):
    world = installed(tmp_path / 'home', template())
    world.ref = {**world.ref, field: '0' * 64}
    result = save(world)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert WorkflowStore(world.owner.home_dir).get_workflow('derived-check') is None


@pytest.mark.parametrize('change', [
    lambda value: value['steps'][0].update(mode='unknown-node'),
    lambda value: value['descriptor'].update(owner_id='other-owner'),
    lambda value: value['descriptor'].update(credential_refs=[{'credential_ref': 'credential:other'}]),
    lambda value: value['descriptor'].update(metadata={'business_workflow': {'protected': True}}),
    lambda value: value['steps'][0]['config'].update(review_id='foreign-review'),
])
def test_original_node_validation_and_reserved_host_fields_are_not_bypassed(tmp_path, change):
    value = template()
    change(value)
    world = installed(tmp_path / 'home', value)
    result = save(world)
    assert not result.ok and result.effect_outcome == 'not_started', result.output
    assert WorkflowStore(world.owner.home_dir).get_workflow('derived-check') is None


def test_strict_json_and_definition_source_exclusion(tmp_path):
    world = installed(tmp_path / 'home', b'{"descriptor":{},"descriptor":{},"steps":[]}')
    assert not save(world).ok
    result = save(world, definition=template())
    assert not result.ok and result.effect_outcome == 'not_started'
    assert 'oneOf' in result.output


def test_expired_and_foreign_owner_source_do_not_read_or_create(tmp_path):
    main = installed(tmp_path / 'home', template())
    other_value = template()
    other_value['body_markdown'] = '另一Owner不同字节'
    other = installed(tmp_path / 'home', other_value, OwnerIdentity.provider_user('feishu', 'other'))
    other.ref = main.ref
    denied = save(other)
    assert not denied.ok and denied.effect_outcome == 'not_started'
    assert WorkflowStore(other.owner.home_dir).get_workflow('derived-check') is None
    main.store.change_activation(PluginActivationRequest('revoke-template', main.entry.revision,
        replace(main.entry.activation, phase='revoked')))
    expired = save(main)
    assert not expired.ok and expired.effect_outcome == 'not_started'


def test_source_cannot_be_forged_cleared_or_changed_by_edit_or_rollback(tmp_path):
    world = installed(tmp_path / 'home', template())
    assert save(world).ok
    original = WorkflowStore(world.owner.home_dir).get_workflow('derived-check').to_dict()
    source = deepcopy(original['descriptor']['metadata']['derived_from'])
    forged = deepcopy(original)
    forged['descriptor']['id'] = 'forged'
    forged['descriptor']['metadata'] = {'derived_from': source}
    denied = world.tool.execute({'action': 'save', 'workflow_id': 'forged', 'definition': forged})
    assert not denied.ok and json.loads(denied.output).get('error_code') == 'WORKFLOW_TEMPLATE_SOURCE_HOST_ONLY', denied.output
    ordinary = deepcopy(original)
    ordinary['descriptor']['metadata'] = {}
    ordinary['descriptor']['name'] = '普通编辑'
    edited = world.tool.execute({'action': 'save', 'workflow_id': 'derived-check', 'definition': ordinary,
        'expected_revision': original['descriptor']['revision']})
    assert edited.ok, edited.output
    assert json.loads(edited.output)['descriptor']['metadata']['derived_from'] == source
    current = json.loads(edited.output)
    rolled = world.tool.execute({'action': 'rollback', 'workflow_id': 'derived-check', 'to_version': 1,
        'expected_revision': current['descriptor']['revision']})
    assert rolled.ok, rolled.output
    assert json.loads(rolled.output)['descriptor']['metadata']['derived_from'] == source


def test_concurrent_derivation_does_not_overwrite_winner(tmp_path):
    world = installed(tmp_path / 'home', template())
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: save(world), range(2)))
    assert sorted(result.ok for result in results) == [False, True]
    assert WorkflowStore(world.owner.home_dir).get_workflow('derived-check').descriptor.revision == 1
