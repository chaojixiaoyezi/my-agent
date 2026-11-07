"""W4 可见性：工具/技能开关只改原策略列表，含分页、强制限制、owner 隔离与并发。

测试纪律：隔离 HOME/MY_AGENT_HOME，不联网、不启动 Agent；并发用 Event/Barrier 控制交错，
不靠长 sleep。插件启停沿原生生命周期，这里只验证未就绪时的结构化拒绝。
"""
from __future__ import annotations

import json
import threading

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')


def _skill(home, name):
    directory = home / 'skills' / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'SKILL.md').write_text(
        '---\nname: ' + name + '\ndescription: fixture skill\n---\nbody\n', encoding='utf-8')
    return directory


@pytest.fixture
def visibility_env(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'isolated-home'))
    root = tmp_path / 'isolated-home'
    hosts = {}
    catalogs = {'main': ['read_file', 'web_fetch', 'web_search', 'write_file']}

    def make_host(owner, tools):
        identity = OwnerIdentity.local_main() if owner == 'main' else OwnerIdentity.local_user(owner)
        paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root, identity))
        paths.owner_home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        agent = SimpleNamespace(tools=SimpleNamespace(tools={name: object() for name in tools}))
        host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True),
                               business_native_admin=lambda: True, conversation_store=None)
        context = api.bind_business_context(host, authorize=lambda action, facts: True,
                                            host_provider=lambda stack: agent)
        return host, context, api.BusinessServices(host)

    def host_for(owner='main'):
        if owner not in hosts:
            hosts[owner] = make_host(owner, catalogs.setdefault(owner, ['read_file']))
        return hosts[owner][0]

    def call(action, params=None, *, owner='main', key=None):
        host, context, services = (host_for(owner), *hosts[owner][1:])
        return V13Call(action, params or {}, 'visibility-request', context.owner_id, context.subject_id,
                       context, services, host.home_paths.owner_home_dir,
                       key or 'visibility-' + uuid4().hex, clock=lambda: 1800000000.0)

    return SimpleNamespace(call=call, host=host_for, root=root, tmp=tmp_path)


def _rows(service, env, params, *, owner='main'):
    return service.visibility_item_get(env.call('visibility.item.get', params, owner=owner))


def test_tool_list_reads_owner_policy_and_keeps_disabled_items(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _write_json(home.owner_home_dir / 'tool_policy.json',
                {'schema_version': 'tool-policy.v1', 'approval_mode': 'ask', 'disabled_tools': ['read_file']})
    page = _rows(service, visibility_env, {'kind': 'tool'})
    assert page['coverage'] == 'complete'
    assert [row['id'] for row in page['items']] == ['read_file', 'web_fetch', 'web_search', 'write_file']
    assert [row['visible'] for row in page['items']] == [False, True, True, True]
    assert {row['source'] for row in page['items']} == {'tool_registry'}
    assert page['items'][0]['kind'] == 'tool'

    toggled = service.visibility_item_toggle(visibility_env.call(
        'visibility.item.toggle', {'kind': 'tool', 'id': 'write_file', 'visible': False}))['item']
    assert toggled == {'kind': 'tool', 'id': 'write_file', 'visible': False, 'source': 'tool_registry'}
    stored = json.loads((home.owner_home_dir / 'tool_policy.json').read_text(encoding='utf-8'))
    assert stored['disabled_tools'] == ['read_file', 'write_file']
    assert stored['approval_mode'] == 'ask' and stored['schema_version'] == 'tool-policy.v1'
    assert _rows(service, visibility_env, {'kind': 'tool', 'id': 'write_file'})['items'] == [toggled]


def test_tool_toggle_can_reenable_a_previously_disabled_item(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _write_json(home.owner_home_dir / 'tool_policy.json',
                {'schema_version': 'tool-policy.v1', 'disabled_tools': ['read_file']})
    item = service.visibility_item_toggle(visibility_env.call(
        'visibility.item.toggle', {'kind': 'tool', 'id': 'read_file', 'visible': True}))['item']
    assert item['visible'] is True
    assert json.loads((home.owner_home_dir / 'tool_policy.json').read_text(encoding='utf-8'))[
        'disabled_tools'] == []
    assert service.visibility_item_toggle(visibility_env.call(
        'visibility.item.toggle', {'kind': 'tool', 'id': 'read_file', 'visible': True}))['item']['visible'] is True


def test_deployment_forced_tool_cannot_be_reenabled(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _write_json(home.owner_home_dir / 'permissions.json',
                {'schema_version': 'permissions.v1', 'network': {'enabled': False}})
    row = _rows(service, visibility_env, {'kind': 'tool', 'id': 'web_search'})['items'][0]
    assert row['visible'] is False
    with pytest.raises(HostAPIError, match='STATE_CONFLICT') as caught:
        service.visibility_item_toggle(visibility_env.call(
            'visibility.item.toggle', {'kind': 'tool', 'id': 'web_search', 'visible': True}))
    assert caught.value.code == 'STATE_CONFLICT'
    assert caught.value.status == 409
    # 拒绝发生在任何写入之前：部署强制项不需要也不允许 owner 侧再记一笔。
    assert not (home.owner_home_dir / 'tool_policy.json').exists()


def test_tool_pagination_uses_hmac_cursor_and_expires_on_garbage(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    first = _rows(service, visibility_env, {'kind': 'tool', 'limit': 2})
    assert [row['id'] for row in first['items']] == ['read_file', 'web_fetch']
    assert first['coverage'] == 'partial' and first['next_cursor']
    second = _rows(service, visibility_env, {'kind': 'tool', 'limit': 2, 'cursor': first['next_cursor']})
    assert [row['id'] for row in second['items']] == ['web_search', 'write_file']
    assert second['coverage'] == 'complete' and 'next_cursor' not in second
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        _rows(service, visibility_env, {'kind': 'tool', 'cursor': 'not-a-cursor'})
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        _rows(service, visibility_env, {'kind': 'tool', 'limit': 101})


def test_tool_unknown_id_is_absent_or_not_found(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    assert _rows(service, visibility_env, {'kind': 'tool', 'id': 'ghost_tool'})['items'] == []
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        service.visibility_item_toggle(visibility_env.call(
            'visibility.item.toggle', {'kind': 'tool', 'id': 'ghost_tool', 'visible': False}))


def test_skill_toggle_uses_disabled_skills_list(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _skill(home.owner_home_dir, 'demo-skill')
    _write_json(home.owner_home_dir / 'skill_policy.json',
                {'schema_version': 'skill-policy.v1', 'enabled': True, 'disabled_skills': []})

    item = service.visibility_skill_toggle(visibility_env.call(
        'visibility.skill.toggle', {'name': 'demo-skill', 'visible': False}))['item']
    assert item['name'] == 'demo-skill' and item['visible'] is False
    stored = json.loads((home.owner_home_dir / 'skill_policy.json').read_text(encoding='utf-8'))
    assert stored['disabled_skills'] == ['demo-skill']
    assert stored['enabled'] is True and stored['schema_version'] == 'skill-policy.v1'

    page = service.visibility_skill_get(visibility_env.call('visibility.skill.get', {'name': 'demo-skill'}))
    assert [(row['name'], row['visible']) for row in page['items']] == [('demo-skill', False)]
    assert page['items'][0]['source'] == 'owner'

    again = service.visibility_skill_toggle(visibility_env.call(
        'visibility.skill.toggle', {'name': 'demo-skill', 'visible': True}))['item']
    assert again['visible'] is True
    assert json.loads((home.owner_home_dir / 'skill_policy.json').read_text(encoding='utf-8'))[
        'disabled_skills'] == []


def test_skill_unknown_name_is_not_found(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        service.visibility_skill_toggle(visibility_env.call(
            'visibility.skill.toggle', {'name': 'no-such-skill', 'visible': False}))
    assert service.visibility_skill_get(visibility_env.call(
        'visibility.skill.get', {'name': 'no-such-skill'}))['items'] == []


def test_owner_isolation_keeps_policies_per_home(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    main_home = visibility_env.host().home_paths
    service.visibility_item_toggle(visibility_env.call(
        'visibility.item.toggle', {'kind': 'tool', 'id': 'read_file', 'visible': False}, owner='alice'))
    alice_home = visibility_env.host('alice').home_paths
    assert json.loads((alice_home.owner_home_dir / 'tool_policy.json').read_text(encoding='utf-8'))[
        'disabled_tools'] == ['read_file']
    assert not (main_home.owner_home_dir / 'tool_policy.json').exists()
    assert _rows(service, visibility_env, {'kind': 'tool', 'id': 'read_file'})['items'][0]['visible'] is True
    alice_rows = _rows(service, visibility_env, {'kind': 'tool'}, owner='alice')
    assert [(row['id'], row['visible']) for row in alice_rows['items']] == [('read_file', False)]


def test_cas_rejects_stale_digest_and_keeps_file(visibility_env):
    from agent_py_agent.agent.user_space.owner_policy import (
        cas_update_owner_tool_policy_disabled_tools,
        owner_policy_document_sha256,
    )

    home = visibility_env.host().home_paths
    _write_json(home.owner_home_dir / 'tool_policy.json',
                {'schema_version': 'tool-policy.v1', 'disabled_tools': []})
    stale = owner_policy_document_sha256(home.owner_tool_policy_json)
    assert cas_update_owner_tool_policy_disabled_tools(
        home, 'read_file', False, expected_sha256=stale) is True
    assert cas_update_owner_tool_policy_disabled_tools(
        home, 'web_search', False, expected_sha256=stale) is False
    stored = json.loads((home.owner_home_dir / 'tool_policy.json').read_text(encoding='utf-8'))
    assert stored['disabled_tools'] == ['read_file']
    assert owner_policy_document_sha256(home.owner_tool_policy_json) != stale


def test_concurrent_toggles_do_not_lose_updates(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _write_json(home.owner_home_dir / 'tool_policy.json',
                {'schema_version': 'tool-policy.v1', 'disabled_tools': []})
    barrier = threading.Barrier(2)
    failures = []

    def worker(tool_id):
        try:
            barrier.wait(timeout=10)
            for _round in range(5):
                service.visibility_item_toggle(visibility_env.call(
                    'visibility.item.toggle', {'kind': 'tool', 'id': tool_id, 'visible': False}))
        except BaseException as error:  # noqa: BLE001 - 线程失败要回传给主断言
            failures.append(error)

    threads = [threading.Thread(target=worker, args=(tool_id,)) for tool_id in ('read_file', 'web_search')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not failures
    stored = json.loads((home.owner_home_dir / 'tool_policy.json').read_text(encoding='utf-8'))
    assert stored['disabled_tools'] == ['read_file', 'web_search']


def test_plugin_paths_report_host_not_ready_without_native_host(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    with pytest.raises(HostAPIError, match='HOST_NOT_READY') as caught:
        service.visibility_item_get(visibility_env.call('visibility.item.get', {'kind': 'plugin'}))
    assert caught.value.code == 'HOST_NOT_READY'
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        service.visibility_item_toggle(visibility_env.call(
            'visibility.item.toggle', {'kind': 'plugin', 'id': 'demo-plugin', 'visible': False}))


def test_invalid_schema_is_rejected_before_any_write(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        _rows(service, visibility_env, {'kind': 'agent'})
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        service.visibility_item_toggle(visibility_env.call(
            'visibility.item.toggle', {'kind': 'tool', 'id': 'read_file', 'visible': 'no'}))
    assert not (home.owner_home_dir / 'tool_policy.json').exists()


def test_forced_tool_also_in_owner_list_cannot_be_reenabled(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _write_json(home.owner_permissions_json, {'schema_version': 'permissions.v1', 'network': {'enabled': False}})
    _write_json(home.owner_tool_policy_json, {'schema_version': 'tool-policy.v1', 'disabled_tools': ['web_search']})
    before = home.owner_tool_policy_json.read_bytes()
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        service.visibility_item_toggle(visibility_env.call('visibility.item.toggle', {
            'kind': 'tool', 'id': 'web_search', 'visible': True}))
    assert home.owner_tool_policy_json.read_bytes() == before


def test_visibility_change_expires_previous_page(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    first = _rows(service, visibility_env, {'kind': 'tool', 'limit': 1})
    service.visibility_item_toggle(visibility_env.call('visibility.item.toggle', {
        'kind': 'tool', 'id': 'write_file', 'visible': False}))
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        _rows(service, visibility_env, {'kind': 'tool', 'limit': 1, 'cursor': first['next_cursor']})


def test_skill_visibility_honors_existing_stable_alias(visibility_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    _skill(home.owner_home_dir, 'alias-skill')
    _write_json(home.owner_skill_policy_json, {'schema_version': 'skill-policy.v1',
                                            'disabled_skills': ['owner:alias-skill']})
    page = service.visibility_skill_get(visibility_env.call('visibility.skill.get', {'name': 'alias-skill'}))
    assert len(page['items']) == 1 and page['items'][0]['visible'] is False
    service.visibility_skill_toggle(visibility_env.call('visibility.skill.toggle', {'name': 'alias-skill', 'visible': True}))
    assert json.loads(home.owner_skill_policy_json.read_text())['disabled_skills'] == []


def test_plugin_toggle_real_http_and_native_installation(tmp_path, monkeypatch):
    from agent_py_agent.tests import test_v13_w0_harness as harness
    from agent_py_agent.tests.test_gap_w4_agent_prompts import w4_registry
    from agent_py_agent.tests.test_v13_w11_plugins import _install

    registry = w4_registry.__wrapped__(monkeypatch)
    assert 'visibility.item.toggle' in registry._ACTIONS
    fixture = harness.v13_native.__wrapped__(tmp_path, monkeypatch)
    native = next(fixture)
    try:
        server, agent = native
        agent.config.gateway_per_user_owner_scoping = False
        agent.config.enable_tools = True
        agent.config.enable_plugins = True
        server.auth_middleware.auth_manager.admin_user_id = 'business-alice'
        _install(agent, agent.home_paths.root / 'seeds', plugin_id='w4-content')
        for visible in (True, False):
            status, result = harness.v13_call(native, 'visibility.item.toggle', {
                'kind': 'plugin', 'id': 'w4-content', 'visible': visible,
            }, user='business-alice', idempotency_key='w4-plugin-' + str(visible) + '-abcdefghijk')
            assert status == 200 and result['error'] is None, result
            assert result['data']['item']['visible'] is visible
            status, listed = harness.v13_call(native, 'visibility.item.get', {
                'kind': 'plugin', 'id': 'w4-content'}, user='business-alice')
            assert status == 200 and listed['data']['items'] == [result['data']['item']], listed
    finally:
        with pytest.raises(StopIteration):
            next(fixture)


@pytest.mark.parametrize('kind', ['tool', 'skill'])
def test_policy_writer_removes_padded_legacy_member(visibility_env, kind):
    from agent_py_agent.agent.business_spaces import gap158_w4_visibility as service

    home = visibility_env.host().home_paths
    if kind == 'tool':
        path, field, value = home.owner_tool_policy_json, 'disabled_tools', 'read_file'
    else:
        _skill(home.owner_home_dir, 'padded-skill')
        path, field, value = home.owner_skill_policy_json, 'disabled_skills', 'owner:padded-skill'
    _write_json(path, {field: [' ' + value + ' ', 'unrelated-old-value'], 'custom': 'preserve'})
    if kind == 'tool':
        result = service.visibility_item_toggle(visibility_env.call('visibility.item.toggle', {
            'kind': 'tool', 'id': 'read_file', 'visible': True}))
    else:
        result = service.visibility_skill_toggle(visibility_env.call('visibility.skill.toggle', {
            'name': 'padded-skill', 'visible': True}))
    assert result['item']['visible'] is True
    assert json.loads(path.read_text()) == {field: ['unrelated-old-value'], 'custom': 'preserve'}
