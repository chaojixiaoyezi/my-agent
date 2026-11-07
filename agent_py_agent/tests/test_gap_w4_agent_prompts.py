"""角色覆盖沿原加载器生效，能力继承必须显式选择；服务测试使用隔离 owner。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_templates import (
    load_role_template_store,
    role_template_detail_text,
    role_template_index_text,
)


def _role_file(directory, payload):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (payload['id'] + '.json')
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def test_partial_overlay_inherits_text_but_not_capabilities(tmp_path):
    directory = tmp_path / 'roles'
    _role_file(directory, {'id': 'coordinator', 'prompt_zh': '只改这段说明'})
    base = load_role_template_store().get('coordinator')
    store = load_role_template_store(directory)
    role = store.get('coordinator')
    assert not store.issues
    assert role.prompt_zh == '只改这段说明'
    assert role.output_contract_zh == base.output_contract_zh
    assert role.use_when_zh == base.use_when_zh
    assert role.can_write is False
    assert role.can_spawn_children is False
    assert role.default_tools == []


def test_capability_inheritance_requires_explicit_true(tmp_path):
    directory = tmp_path / 'roles'
    _role_file(directory, {'id': 'coordinator', 'prompt_zh': '继承', 'inherit_capabilities': True})
    role = load_role_template_store(directory).get('coordinator')
    base = load_role_template_store().get('coordinator')
    assert role.can_spawn_children is True
    assert role.can_write is True
    assert role.default_tools == base.default_tools
    _role_file(directory, {'id': 'coordinator', 'inherit_capabilities': True, 'can_write': False})
    assert load_role_template_store(directory).get('coordinator').can_write is False


def test_disabled_role_is_absent_and_explicit_spawn_rejected(tmp_path):
    directory = tmp_path / 'roles'
    _role_file(directory, {'id': 'worker', 'disabled': True})
    store = load_role_template_store(directory)
    assert not store.issues
    assert store.get('worker') is None
    assert 'worker' not in {role.id for role in store.all()}
    assert '- worker:' not in role_template_index_text(directory)
    assert '- worker:' not in role_template_detail_text(directory, roles=['tester'])
    manager = SubAgentManager(tmp_path / 'runs', role_template_dirs=[directory])
    with pytest.raises(ValueError) as caught:
        manager.create_run(goal='不得派发被禁用角色', role='worker')
    assert caught.value.error_code == 'STATE_CONFLICT'
    assert caught.value.template_id == 'worker'


def test_corrupt_owner_json_keeps_builtin_and_reports_issue(tmp_path):
    directory = tmp_path / 'roles'
    directory.mkdir()
    (directory / 'worker.json').write_text('{broken', encoding='utf-8')
    store = load_role_template_store(directory)
    assert store.get('worker').source == 'builtin'
    assert len(store.issues) == 1
    assert store.issues[0].source_path.endswith('worker.json')


def test_legacy_complete_enough_template_keeps_safe_capability_defaults(tmp_path):
    base = load_role_template_store().get('coordinator')
    payload = {key: getattr(base, key) for key in (
        'id', 'name_zh', 'summary_zh', 'scope', 'handles_multiple_targets',
        'use_when_zh', 'output_contract_zh',
    )}
    directory = tmp_path / 'roles'
    _role_file(directory, payload)
    role = load_role_template_store(directory).get('coordinator')
    assert role.can_write is False
    assert role.can_spawn_children is False
    assert role.default_tools == []


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
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
    hosts = {}

    def call(action, params=None, *, owner='main', key=None):
        if owner not in hosts:
            identity = OwnerIdentity.local_main() if owner == 'main' else OwnerIdentity.local_user(owner)
            paths = home_paths_with_owner(home_paths(tmp_path / 'isolated-home'),
                                          resolve_owner_home(tmp_path / 'isolated-home', identity))
            paths.owner_home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True,
                                                                          subagent_role_template_dirs=[]))
            context = api.bind_business_context(host, authorize=lambda action, facts: True)
            hosts[owner] = (host, context, api.BusinessServices(host))
        host, context, services = hosts[owner]
        return V13Call(action, params or {}, 'prompt-request', context.owner_id, context.subject_id,
                       context, services, host.home_paths.owner_home_dir, key or 'prompt-' + uuid4().hex,
                       clock=lambda: 1800000000.0)

    return SimpleNamespace(call=call, hosts=hosts, root=tmp_path)


def test_prompt_set_get_reset_history_and_replay(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    original = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    assert original['sha256'] == 'absent'
    request = prompt_env.call('agent_prompts.set', {'role': 'worker', 'prompt': '新版说明', 'expected_sha256': 'absent'})
    updated = service.agent_prompts_set(request)['prompt']
    assert updated['prompt'] == '新版说明' and updated['source'] == 'user'
    assert service.agent_prompts_set(request)['prompt'] == updated
    assert service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt'] == updated
    with pytest.raises(HostAPIError, match='REVISION_CONFLICT'):
        service.agent_prompts_set(prompt_env.call('agent_prompts.set', request.params))
    reset = service.agent_prompts_reset(prompt_env.call('agent_prompts.reset', {
        'role': 'worker', 'expected_sha256': updated['sha256'],
    }))['prompt']
    assert reset['prompt'] == original['prompt']
    history = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker'}))
    assert [row['kind'] for row in history['items']] == ['reset', 'set']
    assert len(history['items'][0]['sha256']) == 64
    assert load_role_template_store(__import__('pathlib').Path(reset['layer_path']).parent).get('worker').can_write is True


def test_preview_and_variables_are_read_only(prompt_env):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    call = prompt_env.call('agent_prompts.preview', {'role': 'worker', 'prompt': '预览{goal}', 'wrapup': '只看预览'})
    before = sorted(str(path) for path in call.owner_home.iterdir())
    preview = service.agent_prompts_preview(call)
    assert '预览{goal}' in preview['rendered_detail'] and '只看预览' in preview['rendered_detail']
    assert preview['effective_source'] == 'builtin'
    assert sorted(str(path) for path in call.owner_home.iterdir()) == before
    assert not (call.owner_home / '.agent').exists()
    assert not (call.owner_home / 'data' / 'business_v13').exists()
    original = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    assert original['prompt'] != '预览{goal}'
    assert not Path(original['layer_path']).exists()
    variables = service.agent_prompts_variables(prompt_env.call('agent_prompts.variables'))
    assert variables['variables'] == [] and 'output_contract_zh' in variables['editable_fields']


def test_wrapup_and_triggers_share_one_cas_file_and_stable_ids(prompt_env):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    wrapped = service.agent_prompts_wrapup_set(prompt_env.call('agent_prompts.wrapup.set', {
        'role': 'worker', 'wrapup': '交付证据', 'expected_sha256': 'absent',
    }))
    before = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    first_ids = [row['trigger_id'] for row in before['items']]
    created = service.agent_prompts_triggers_create(prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': '新增触发条件', 'expected_sha256': wrapped['sha256'],
    }))
    identifier = created['trigger']['trigger_id']
    assert first_ids == [row['trigger_id'] for row in service.agent_prompts_triggers_list(
        prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))['items'] if row['trigger_id'] != identifier]
    changed = service.agent_prompts_triggers_update(prompt_env.call('agent_prompts.triggers.update', {
        'role': 'worker', 'trigger_id': identifier, 'text': '更新条件', 'expected_sha256': created['sha256'],
    }))
    import hashlib
    assert changed['trigger']['trigger_id'] == hashlib.sha256('worker|use_when|更新条件'.encode()).hexdigest()[:16]
    identifier = changed['trigger']['trigger_id']
    with pytest.raises(HostAPIError, match='REVISION_CONFLICT'):
        service.agent_prompts_wrapup_set(prompt_env.call('agent_prompts.wrapup.set', {
            'role': 'worker', 'wrapup': '旧版不得覆盖', 'expected_sha256': wrapped['sha256'],
        }))
    deleted = service.agent_prompts_triggers_delete(prompt_env.call('agent_prompts.triggers.delete', {
        'role': 'worker', 'trigger_id': first_ids[0], 'expected_sha256': changed['sha256'],
    }))
    assert deleted['deleted'] is True
    final = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    assert identifier in {row['trigger_id'] for row in final['items']}
    current = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    data = json.loads(Path(current['layer_path']).read_text())
    assert data['output_contract_zh'] == '交付证据'
    assert '更新条件' in data['use_when_zh']
    assert service.agent_prompts_wrapup_get(prompt_env.call('agent_prompts.wrapup.get', {'role': 'worker'}))['wrapup'] == '交付证据'


def test_prompt_history_is_bounded_and_cursor_is_owner_bound(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.conversation.business_access import BusinessError

    sha = 'absent'
    bound = service._limit('AGENT_PROMPT_HISTORY_MAX_PER_ROLE')
    for index in range(bound + 2):
        sha = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
            'role': 'worker', 'prompt': str(index) + '正文' * 400, 'expected_sha256': sha,
        }))['prompt']['sha256']
    first = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker', 'limit': 1}))
    assert first['coverage'] == 'partial' and first['items'][0]['version'] == bound + 2
    assert len(first['items'][0]['prompt_preview']) == 500
    full = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker'}))
    assert len(full['items']) == bound
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        service.agent_prompts_history(prompt_env.call('agent_prompts.history', {
            'role': 'worker', 'cursor': first['next_cursor'], 'limit': 1,
        }, owner='bob'))


def test_trigger_limit_and_foreign_id_are_not_found(prompt_env, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    original = service._limit
    monkeypatch.setattr(service, '_limit', lambda name: 2 if name == 'PROMPT_TRIGGERS_MAX_PER_ROLE' else original(name))
    with pytest.raises(HostAPIError, match='LIMIT_EXCEEDED'):
        service.agent_prompts_triggers_create(prompt_env.call('agent_prompts.triggers.create', {
            'role': 'worker', 'text': '超额', 'kind': 'use_when', 'expected_sha256': 'absent',
        }))
    monkeypatch.setattr(service, '_limit', original)
    created = service.agent_prompts_triggers_create(prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'text': '本人', 'kind': 'use_when', 'expected_sha256': 'absent',
    }))
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        service.agent_prompts_triggers_update(prompt_env.call('agent_prompts.triggers.update', {
            'role': 'worker', 'trigger_id': created['trigger']['trigger_id'], 'text': '不得跨用户',
            'expected_sha256': 'absent',
        }, owner='bob'))


@pytest.mark.parametrize('role', ['../worker', 'a/b', '..', '/tmp/worker', 'a\\b', 'a\x00b'])
def test_role_path_safety_before_filename(prompt_env, role):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
            'role': role, 'prompt': '不能写', 'expected_sha256': 'absent',
        }))


def test_owner_override_never_leaks_to_another_owner(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    a = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': 'A私有说明', 'expected_sha256': 'absent',
    }))['prompt']
    b = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}, owner='bob'))['prompt']
    assert b['source'] == 'builtin' and b['sha256'] == 'absent'
    assert b['prompt'] != a['prompt'] and b['layer_path'] != a['layer_path']


def test_concurrent_same_sha_has_only_one_winner(prompt_env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    entered, release, contender = Event(), Event(), Event()
    original = service.write_bytes_atomic_beneath
    first = prompt_env.call('agent_prompts.set', {'role': 'worker', 'prompt': 'first', 'expected_sha256': 'absent'})
    second = prompt_env.call('agent_prompts.set', {'role': 'worker', 'prompt': 'second', 'expected_sha256': 'absent'})

    def delayed(root, parts, raw, **kwargs):
        if parts[-1] == 'worker.json':
            entered.set()
            assert release.wait(5)
        return original(root, parts, raw, **kwargs)

    def losing_writer():
        contender.set()
        with pytest.raises(HostAPIError, match='REVISION_CONFLICT'):
            service.agent_prompts_set(second)

    monkeypatch.setattr(service, 'write_bytes_atomic_beneath', delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(service.agent_prompts_set, first)
        try:
            assert entered.wait(5)
            loser = pool.submit(losing_writer)
            assert contender.wait(5)
        finally:
            release.set()
        assert winner.result(timeout=5)['prompt']['prompt'] == 'first'
        loser.result(timeout=5)
    assert service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']['prompt'] == 'first'


def test_file_publication_before_history_failure_retries_without_duplicate_trigger(prompt_env, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    request = prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': '只创建一次', 'expected_sha256': 'absent',
    })
    original = V13Store._materialized

    def fail_before_history_commit(self, pending):
        path = request.owner_home / '.agent' / 'subagents' / 'roles' / 'worker.json'
        if path.exists() and '只创建一次' in path.read_text():
            raise OSError('injected-before-history-journal')
        return original(self, pending)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, '_materialized', fail_before_history_commit)
        with pytest.raises(HostAPIError) as caught:
            service.agent_prompts_triggers_create(request)
        assert caught.value.outcome == 'unknown'
    assert V13Store._materialized is original
    before = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    recovered = service.agent_prompts_triggers_create(request)
    after = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    assert before == after
    assert recovered['trigger']['trigger_id'] in {row['trigger_id'] for row in after['items']}
    assert len([row for row in after['items'] if row['text'] == '只创建一次']) == 1
    history = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker'}))
    assert len(history['items']) == 1 and history['items'][0]['sha256'] == recovered['sha256']


def test_existing_single_file_name_and_bundle_are_updated_in_place(prompt_env):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    call = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    directory = call.owner_home / '.agent' / 'subagents' / 'roles'
    directory.mkdir(parents=True)
    file = directory / 'custom-catalog.json'
    file.write_text(json.dumps({'templates': [
        {'id': 'worker', 'prompt_zh': '旧说明', 'inherit_capabilities': True},
        {'id': 'tester', 'prompt_zh': '另一个角色保留', 'inherit_capabilities': True},
    ]}))
    current = service.agent_prompts_get(call)['prompt']
    assert Path(current['layer_path']) == file
    updated = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': '新说明', 'expected_sha256': current['sha256'],
    }))['prompt']
    assert updated['prompt'] == '新说明'
    assert json.loads(file.read_text())['templates'][1]['prompt_zh'] == '另一个角色保留'
    assert not (directory / 'worker.json').exists()


def test_read_only_deployment_layer_can_be_previewed_but_not_written(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    call = prompt_env.call('agent_prompts.preview', {'role': 'worker', 'prompt': '仅内存预览'})
    external = prompt_env.root / 'deployment-role-layer'
    external.mkdir()
    call.context.host.config.subagent_role_template_dirs = [str(external)]
    assert '仅内存预览' in service.agent_prompts_preview(call)['rendered_detail']
    assert service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']['sha256'] == ''
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
            'role': 'worker', 'prompt': '不得写部署层', 'expected_sha256': 'absent',
        }))
    assert list(external.iterdir()) == []


def test_foreign_owner_directory_and_file_links_never_expose_prompt(prompt_env):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    private = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': 'B不能泄露的内容', 'expected_sha256': 'absent',
    }, owner='bob'))['prompt']
    a = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    directory = a.owner_home / '.agent' / 'subagents' / 'roles'
    directory.parent.mkdir(parents=True)
    directory.symlink_to(Path(private['layer_path']).parent, target_is_directory=True)
    assert 'B不能泄露的内容' not in service.agent_prompts_get(a)['prompt']['prompt']
    directory.unlink()
    directory.mkdir()
    (directory / 'worker.json').symlink_to(private['layer_path'])
    assert 'B不能泄露的内容' not in service.agent_prompts_get(a)['prompt']['prompt']


def test_shadowed_first_writer_rejected_before_publication(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    call = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    first, second = call.owner_home / 'first', call.owner_home / 'second'
    first.mkdir()
    _role_file(second, {'id': 'worker', 'prompt_zh': '后层生效', 'inherit_capabilities': True})
    call.context.host.config.subagent_role_template_dirs = [str(first), str(second)]
    before = service.agent_prompts_get(call)['prompt']
    with pytest.raises(HostAPIError, match='REVISION_CONFLICT'):
        service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
            'role': 'worker', 'prompt': '不得谎称生效', 'expected_sha256': before['sha256'],
        }))
    assert list(first.iterdir()) == []
    assert service.agent_prompts_get(call)['prompt']['prompt'] == '后层生效'


def test_delete_last_use_when_preserves_other_overrides(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    updated = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': '原覆盖不能消失', 'expected_sha256': 'absent',
    }))['prompt']
    rows = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))['items']
    identifier = next(row['trigger_id'] for row in rows if row['kind'] == 'use_when')
    service.agent_prompts_triggers_delete(prompt_env.call('agent_prompts.triggers.delete', {
        'role': 'worker', 'trigger_id': identifier, 'expected_sha256': updated['sha256'],
    }))
    after = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))['items']
    assert not [row for row in after if row['kind'] == 'use_when']
    assert service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']['prompt'] == '原覆盖不能消失'


def test_reset_removes_all_soft_fields_and_empty_legacy_file(prompt_env):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    call = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    original = service.agent_prompts_get(call)['prompt']
    path = Path(original['layer_path'])
    _role_file(path.parent, {'id': 'worker', 'prompt_zh': '自定义提示', 'output_contract_zh': '自定义收尾',
                            'use_when_zh': ['自定义条件'], 'do_not_use_when_zh': ['自定义排除']})
    before = service.agent_prompts_get(call)['prompt']
    after = service.agent_prompts_reset(prompt_env.call('agent_prompts.reset', {
        'role': 'worker', 'expected_sha256': before['sha256'],
    }))['prompt']
    assert not path.exists()
    assert after['prompt'] == original['prompt'] and after['wrapup'] == original['wrapup']
    assert after['sha256'] == 'absent' and after['source'] == 'builtin'


def test_later_edit_does_not_erase_recoverable_original_receipt(prompt_env, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    request = prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': 'K1原结果', 'expected_sha256': 'absent',
    })
    materialized = V13Store._materialized

    def fail_after_file(self, pending):
        path = request.owner_home / '.agent' / 'subagents' / 'roles' / 'worker.json'
        if path.exists() and 'K1原结果' in path.read_text():
            raise OSError('injected-after-file-before-history')
        return materialized(self, pending)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, '_materialized', fail_after_file)
        with pytest.raises(HostAPIError) as caught:
            service.agent_prompts_triggers_create(request)
        assert caught.value.code == 'OUTCOME_UNKNOWN'
    first = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    identifier = next(row['trigger_id'] for row in first['items'] if row['text'] == 'K1原结果')
    service.agent_prompts_wrapup_set(prompt_env.call('agent_prompts.wrapup.set', {
        'role': 'worker', 'wrapup': 'K2后来编辑', 'expected_sha256': first['sha256'],
    }))
    recovered = service.agent_prompts_triggers_create(request)
    assert recovered['trigger']['trigger_id'] == identifier and recovered['sha256'] == first['sha256']
    assert service.agent_prompts_wrapup_get(prompt_env.call('agent_prompts.wrapup.get', {'role': 'worker'}))['wrapup'] == 'K2后来编辑'


def test_bundle_id_normalization_matches_loader(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    call = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    directory = call.owner_home / '.agent' / 'subagents' / 'roles'
    directory.mkdir(parents=True)
    (directory / 'catalog.json').write_text(json.dumps({'templates': [{'id': 'Worker', 'prompt_zh': '大写角色'}]}))
    before = service.agent_prompts_get(call)['prompt']
    after = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': '更新大写角色', 'expected_sha256': before['sha256'],
    }))['prompt']
    assert after['prompt'] == '更新大写角色'


@pytest.fixture
def w4_registry(monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13

    # W0 负责生产静态表；未合入时仍用同一加载器在测试中声明该已交付分片。
    name = 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w4'
    modules = (*v13.V13_SLICE_MODULES, name) if name not in v13.V13_SLICE_MODULES else v13.V13_SLICE_MODULES
    loaded = v13._load_registry(modules)
    for field, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), loaded):
        monkeypatch.setattr(v13, field, value)
    return v13


def test_agent_visibility_roundtrip_and_deployment_restriction(prompt_env):
    import hashlib

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    request = prompt_env.call('visibility.agent.set', {'key': 'worker', 'visible': False})
    disabled = service.visibility_agent_set(request)['item']
    assert disabled['visible'] is False
    assert service.visibility_agent_set(request)['item'] == disabled
    directory = request.owner_home / '.agent' / 'subagents' / 'roles'
    assert load_role_template_store(directory).get('worker') is None
    assert service.visibility_agent_get(prompt_env.call('visibility.agent.get', {'key': 'worker'}))['items'] == [disabled]
    service.visibility_agent_set(prompt_env.call('visibility.agent.set', {
        'key': 'worker', 'visible': True, 'expected_sha256': disabled['sha256'],
    }))
    assert load_role_template_store(directory).get('worker') is not None
    deployment = prompt_env.root / 'deployment'
    _role_file(deployment, {'id': 'worker', 'disabled': True})
    request.context.host.config.subagent_role_template_dirs = [str(deployment), str(directory)]
    current = service.visibility_agent_get(prompt_env.call('visibility.agent.get', {'key': 'worker'}))['items'][0]
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        service.visibility_agent_set(prompt_env.call('visibility.agent.set', {
            'key': 'worker', 'visible': True, 'expected_sha256': current['sha256'],
        }))
    assert hashlib.sha256((directory / 'worker.json').read_bytes()).hexdigest() == current['sha256']


def test_real_business_dispatcher_validates_w4_dtos_and_cas(prompt_env, w4_registry):
    from agent_py_agent.agent.gateway_parts import business_api as api

    def invoke(action, params=None, key='wire-key-abcdefghijkl'):
        call = prompt_env.call(action, params, key=key)
        request = {'action': action, 'params': params or {}, 'request_id': call.request_id}
        if action.endswith(('.set', '.reset', '.create', '.update', '.delete')):
            request['idempotency_key'] = key
        return api.execute_business(request, call.context, call.services)

    status, first = invoke('agent_prompts.get', {'role': 'worker'})
    assert status == 200 and first['error'] is None, first
    sha = first['data']['prompt']['sha256']
    status, changed = invoke('agent_prompts.set', {'role': 'worker', 'prompt': '真实业务入口', 'expected_sha256': sha})
    assert status == 200 and changed['data']['prompt']['prompt'] == '真实业务入口', changed
    status, conflict = invoke('agent_prompts.set', {'role': 'worker', 'prompt': '不得覆盖', 'expected_sha256': sha}, key='wire-key-qrstuvwxyz')
    assert status == 409 and conflict['error']['code'] == 'REVISION_CONFLICT', conflict
    status, invalid = invoke('agent_prompts.get', {'role': 'worker', 'owner_id': 'local/bob'})
    assert status == 400 and invalid['error']['code'] == 'INVALID_SCHEMA'
    status, preview = invoke('agent_prompts.preview', {'role': 'worker', 'prompt': '只在内存'})
    assert status == 200 and '只在内存' in preview['data']['rendered_detail'], preview
    status, wrapup = invoke('agent_prompts.wrapup.set', {
        'role': 'worker', 'wrapup': '标准收尾', 'expected_sha256': changed['data']['prompt']['sha256'],
    }, key='wrapup-key-abcdefghijkl')
    assert status == 200 and wrapup['data']['wrapup'] == '标准收尾', wrapup
    status, created = invoke('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': '标准触发器', 'expected_sha256': wrapup['data']['sha256'],
    }, key='trigger-key-abcdefghijkl')
    assert status == 200 and created['data']['trigger']['text'] == '标准触发器', created
    status, history = invoke('agent_prompts.history', {'role': 'worker'})
    assert status == 200 and len(history['data']['items']) == 3, history
    status, variables = invoke('agent_prompts.variables')
    assert status == 200 and variables['data']['variables'] == [], variables
    status, visibility = invoke('visibility.agent.set', {'key': 'worker', 'visible': False}, key='visibility-key-abcdefghijkl')
    assert status == 200 and visibility['data']['item']['visible'] is False, visibility


def test_gateway_http_w4_preview_uses_real_route(w4_registry, tmp_path, monkeypatch):
    from agent_py_agent.tests import test_v13_w0_harness as harness

    # 复用真实 fixture 的生成器，不自建运输层或伪造返回值。
    server_fixture = harness.v13_native.__wrapped__(tmp_path, monkeypatch)
    native = next(server_fixture)
    try:
        status, body = harness.v13_call(native, 'agent_prompts.preview', {
            'role': 'worker', 'prompt': 'HTTP真实往返正文', 'wrapup': 'HTTP收尾',
        }, user='business-alice')
        assert status == 200 and body['error'] is None, body
        assert 'HTTP真实往返正文' in body['data']['rendered_detail']
        assert 'HTTP收尾' in body['data']['rendered_detail']
    finally:
        with pytest.raises(StopIteration):
            next(server_fixture)


def test_get_content_and_sha_are_same_observation_when_file_replaced(prompt_env, monkeypatch):
    import hashlib
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    original = service.agent_prompts_set(prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': '读取第一版', 'expected_sha256': 'absent',
    }))['prompt']
    path = Path(original['layer_path'])
    reader = service.read_bytes_beneath
    changed = False

    def replace_before_sha(root, parts):
        nonlocal changed
        if not changed and parts[-1] == 'worker.json':
            changed = True
            payload = json.loads(path.read_text())
            payload['prompt_zh'] = '读取第二版'
            path.write_text(json.dumps(payload))
        return reader(root, parts)

    monkeypatch.setattr(service, 'read_bytes_beneath', replace_before_sha)
    current = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    assert changed and current['prompt'] == '读取第二版'
    assert current['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_trigger_ids_follow_exact_contract_hash(prompt_env):
    import hashlib

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    before = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    for row in before['items']:
        assert row['trigger_id'] == hashlib.sha256(f"{row['role']}|{row['kind']}|{row['text']}".encode()).hexdigest()[:16]


def test_first_publication_during_get_reloads_content_and_sha(prompt_env, monkeypatch):
    import hashlib

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service

    call = prompt_env.call('agent_prompts.get', {'role': 'worker'})
    directory = call.owner_home / '.agent' / 'subagents' / 'roles'
    reader = service.read_bytes_beneath
    published = False

    def publish_before_read(root, parts):
        nonlocal published
        if not published and parts[-1] == 'worker.json':
            published = True
            _role_file(directory, {'id': 'worker', 'prompt_zh': '并发首次发布'})
        return reader(root, parts)

    monkeypatch.setattr(service, 'read_bytes_beneath', publish_before_read)
    current = service.agent_prompts_get(call)['prompt']
    assert published and current['prompt'] == '并发首次发布'
    assert current['sha256'] == hashlib.sha256((directory / 'worker.json').read_bytes()).hexdigest()


def test_trigger_whitespace_is_normalized_before_hash_and_empty_rejected(prompt_env):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    request = prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': ' 条件 ', 'expected_sha256': 'absent',
    })
    created = service.agent_prompts_triggers_create(request)
    listed = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    assert created['trigger'] in listed['items'] and created['trigger']['text'] == '条件'
    updated = service.agent_prompts_triggers_update(prompt_env.call('agent_prompts.triggers.update', {
        'role': 'worker', 'trigger_id': created['trigger']['trigger_id'],
        'text': ' 新条件 ', 'expected_sha256': created['sha256'],
    }))
    assert updated['trigger']['text'] == '新条件'
    for action in ('create', 'update'):
        params = {'role': 'worker', 'text': '  \t ', 'expected_sha256': updated['sha256']}
        params.update({'kind': 'use_when'} if action == 'create' else {
            'trigger_id': updated['trigger']['trigger_id']})
        with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
            getattr(service, 'agent_prompts_triggers_' + action)(prompt_env.call('agent_prompts.triggers.' + action, params))


def test_deleted_reset_recovery_survives_later_edit(prompt_env, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    original = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    path = Path(original['layer_path'])
    _role_file(path.parent, {'id': 'worker', 'prompt_zh': '删除后恢复的旧字段'})
    before = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    reset = prompt_env.call('agent_prompts.reset', {'role': 'worker', 'expected_sha256': before['sha256']})
    materialized = V13Store._materialized

    def fail_after_delete(self, pending):
        if not path.exists():
            raise OSError('injected-after-unlink-before-history')
        return materialized(self, pending)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, '_materialized', fail_after_delete)
        with pytest.raises(HostAPIError, match='OUTCOME_UNKNOWN'):
            service.agent_prompts_reset(reset)
    assert not path.exists()
    service.agent_prompts_wrapup_set(prompt_env.call('agent_prompts.wrapup.set', {
        'role': 'worker', 'wrapup': '后续编辑不能覆盖原回执', 'expected_sha256': 'absent',
    }))
    recovered = service.agent_prompts_reset(reset)['prompt']
    assert recovered['prompt'] == original['prompt'] and recovered['sha256'] == 'absent'
    assert service.agent_prompts_wrapup_get(prompt_env.call('agent_prompts.wrapup.get', {
        'role': 'worker'}))['wrapup'] == '后续编辑不能覆盖原回执'
    history = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker'}))
    assert [row['kind'] for row in history['items']] == ['wrapup', 'reset']


def test_unknown_file_publication_keeps_idempotency_parameter_binding(prompt_env, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    request = prompt_env.call('agent_prompts.triggers.create', {
        'role': 'worker', 'kind': 'use_when', 'text': '原参数条件', 'expected_sha256': 'absent',
    }, key='same-key-abcdefghijkl')
    materialized = V13Store._materialized

    def fail_after_file(self, pending):
        path = request.owner_home / '.agent' / 'subagents' / 'roles' / 'worker.json'
        if path.exists() and '原参数条件' in path.read_text():
            raise OSError('injected-after-file')
        return materialized(self, pending)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, '_materialized', fail_after_file)
        with pytest.raises(HostAPIError, match='OUTCOME_UNKNOWN'):
            service.agent_prompts_triggers_create(request)
    before = service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'}))
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        service.agent_prompts_triggers_create(prompt_env.call('agent_prompts.triggers.create', {
            'role': 'worker', 'kind': 'use_when', 'text': '不得复用键的新条件', 'expected_sha256': before['sha256'],
        }, key=request.idempotency_key))
    assert service.agent_prompts_triggers_list(prompt_env.call('agent_prompts.triggers.list', {'role': 'worker'})) == before
    assert service.agent_prompts_triggers_create(request)['trigger']['text'] == '原参数条件'


def test_deleted_reset_preserves_previous_unknown_set_receipt(prompt_env, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import gap158_w4_agent_prompts as service
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    original = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    path = Path(original['layer_path'])
    _role_file(path.parent, {'id': 'worker', 'prompt_zh': 'legacy旧说明'})
    before = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    request = prompt_env.call('agent_prompts.set', {
        'role': 'worker', 'prompt': 'K1发布后的说明', 'expected_sha256': before['sha256'],
    })
    materialized = V13Store._materialized

    def fail_after_publication(self, pending):
        if path.exists() and 'K1发布后的说明' in path.read_text():
            raise OSError('injected-published-set')
        return materialized(self, pending)

    with monkeypatch.context() as patch:
        patch.setattr(V13Store, '_materialized', fail_after_publication)
        with pytest.raises(HostAPIError, match='OUTCOME_UNKNOWN'):
            service.agent_prompts_set(request)
    published = service.agent_prompts_get(prompt_env.call('agent_prompts.get', {'role': 'worker'}))['prompt']
    service.agent_prompts_reset(prompt_env.call('agent_prompts.reset', {
        'role': 'worker', 'expected_sha256': published['sha256'],
    }))
    assert not path.exists()
    assert service.agent_prompts_set(request)['prompt'] == published
    assert not path.exists()
    history = service.agent_prompts_history(prompt_env.call('agent_prompts.history', {'role': 'worker'}))
    assert [row['kind'] for row in history['items']] == ['reset', 'set']
