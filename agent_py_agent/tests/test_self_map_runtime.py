"""沿真实registry、当前owner和冻结scope验自我目录，不调用外部服务。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import ToolAvailability, ToolInvocationContext
from agent_py_agent.agent.tooling.self_map_registry import SelfMapReadContext
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.fixture
def make_agent(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    def make(*, kind='main', **overrides):
        cfg = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'),
            my_agent_owner_provider='local' if kind == 'main' else 'feishu',
            my_agent_owner_kind=kind, my_agent_owner_id='local/main' if kind == 'main' else 'self-only',
            execution_mode='local_unmanaged', **overrides)
        return SimpleAgent(cfg, tmp_path / ('workspace-' + kind))
    return make


def _read(agent, section=None, *, item=None, allowed=None, limit=100):
    params = {'section': section} if section else {}
    if item:
        params['item'] = item
    elif section:
        params['limit'] = limit
    context = ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=allowed))
    result = agent.tools.tools['self_map'].execute_scoped(params, context)
    assert result.ok, result.output
    return json.loads(result.output)


def _bytes(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


def test_overview_and_all_domains_are_read_only_without_body_or_network(make_agent, tmp_path, monkeypatch):
    agent = make_agent()
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('network'))
    monkeypatch.setattr(agent.skills_service, 'snapshot_for', lambda *a, **k: pytest.fail('reload'))
    before = _bytes(tmp_path)
    overview = _read(agent)
    sections = [row['section'] for row in overview['sections']]
    assert set(sections) == {'directories', 'tools', 'skills', 'packages', 'plugins', 'mcp',
                             'memory', 'models', 'config', 'work', 'permissions', 'context'}
    for section in sections:
        _read(agent, section)
    assert _bytes(tmp_path) == before
    assert next(row for row in overview['sections'] if row['section'] == 'skills')['count'] is None
    work = _read(agent, 'work')
    queue = next(row for row in work['items'] if row['id'] == 'provider_queue')
    assert queue['state'] == 'not_observed' and queue['count'] is None


def test_tools_keep_main_disabled_unavailable_and_scoped_reasons(make_agent, monkeypatch):
    agent = make_agent()
    agent.tools.disabled_tool_names.add('read_file')
    monkeypatch.setattr(agent.tools.tools['write_file'], 'availability',
                        lambda: ToolAvailability(False, 'TOOL_UNAVAILABLE', '当前未配置'))
    allowed = ['self_map', 'list_tools', 'tool_search', 'read_file', 'write_file']
    listing = execute_registry_test_call(agent.tools, 'list_tools', {}, allowed_tools=allowed)
    assert listing.ok, listing.output
    original = json.loads(listing.output)['self_context']['tool_states']
    rows = _read(agent, 'tools', allowed=allowed)['items']
    assert {row['id'] for row in rows} == set(allowed)
    assert {row['id']: (row['state'], row['error_code'], row['reason']) for row in rows} == {
        row['name']: (row['state'], row['error_code'], row['reason']) for row in original}
    assert next(row for row in rows if row['id'] == 'write_file')['reason'] == '当前未配置'
    assert 'admin_controls' not in json.dumps(rows)


def test_tools_loading_matches_current_thread_and_does_not_mutate_discovery(make_agent):
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
    agent = make_agent()
    agent.tools.catalog_deferred_categories = ['filesystem']
    thread = agent.conversation_store.threads.get_or_create({'channel': 'local', 'channel_conversation_id': 'own'})
    agent._current_run_params = RunParams(task_attributes={'conversation_thread_id': thread.thread_id})
    before = _read(agent, 'tools')
    assert next(row for row in before['items'] if row['id'] == 'read_file')['state'] == 'deferred'
    persist_discovered_tools(agent, agent._current_run_params, {'tool_result_envelope': {
        'tool_search': {'loaded_tool_names': ['read_file']}}})
    saved = agent.conversation_store.threads.load(thread.thread_id)
    after = _read(agent, 'tools')
    assert next(row for row in after['items'] if row['id'] == 'read_file')['state'] == 'ready'
    assert agent.conversation_store.threads.load(thread.thread_id) == saved


def test_new_tool_category_uses_original_metadata_without_closed_enum(make_agent):
    agent = make_agent()
    tool = agent.tools.tools['read_file']
    tool.model_spec = replace(tool.model_spec, hints=replace(tool.model_spec.hints, category='future_data'))
    value = _read(agent, 'tools', allowed=['self_map', 'read_file'])
    row = next(row for row in value['items'] if row['id'] == 'read_file')
    assert row['group'] == 'future_data' and row['state'] == 'ready'
    assert value['completeness'] == 'complete'


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_nonadmin_does_not_read_admin_install_or_rule_inventory(make_agent, monkeypatch, kind):
    agent = make_agent(kind=kind)
    monkeypatch.setattr('agent_py_agent.agent.plugin_self_map.PluginInstallStore',
                        lambda *a, **k: pytest.fail('admin inventory read'))
    agent.agents_source_repository = SimpleNamespace(configuration=lambda: pytest.fail('parent rule source read'))
    allowed = ['self_map', 'list_tools', 'tool_search', 'read_file']
    assert _read(agent, 'plugins', allowed=allowed)['items'] == []
    assert _read(agent, 'memory', allowed=allowed)['items'] == []
    assert _read(agent, 'config', allowed=allowed)['items'] == []
    encoded = json.dumps(_read(agent, 'tools', allowed=allowed))
    assert 'admin_controls' not in encoded and 'restart_gateway' not in encoded
    assert 'rules_source' not in json.dumps(_read(agent, 'memory', allowed=allowed))


def test_unbound_group_actor_cannot_read_schedules(make_agent, monkeypatch):
    agent = make_agent(kind='group')
    monkeypatch.setattr(agent.scheduler_repository, 'list_jobs', lambda: pytest.fail('unbound group inventory'))
    work = _read(agent, 'work', allowed=['self_map', 'schedule'])
    assert work['completeness'] == 'partial' and work['total'] is None
    assert 'SELF_MAP_WORK_SOURCE_UNAVAILABLE' in work['errors']


def test_child_uses_exact_thread_and_restricts_parent_inventories(make_agent, monkeypatch):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    agent = make_agent()
    parent = agent.conversation_store.threads.get_or_create({'channel': 'local', 'channel_conversation_id': 'parent'})
    child = agent.conversation_store.threads.get_or_create({'channel': 'local', 'channel_conversation_id': 'child'})
    agent._current_run_params = RunParams(context_scope='task_local', task_attributes={
        'conversation_thread_id': parent.thread_id, 'agent_thread_id': child.thread_id})
    previous = set_current_subagent_context(agent, run_id='child-run')
    monkeypatch.setattr('agent_py_agent.agent.plugin_self_map.PluginInstallStore', lambda *a, **k: pytest.fail('parent inventory'))
    agent.agents_source_repository = SimpleNamespace(configuration=lambda: pytest.fail('parent rules'))
    try:
        context = SelfMapReadContext.for_call(agent, ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=['self_map'])))
        assert context.child and not context.admin and context.thread_id == child.thread_id
        for section in ['memory', 'plugins', 'config']:
            assert _read(agent, section, allowed=['self_map'])['items'] == []
        assert len(_read(agent, 'models')['items']) == 1
        directories = _read(agent, 'directories')['items']
        assert not {'owner_home', 'tasks', 'artifacts', 'builtin_skills'} & {row['id'] for row in directories}
        agent._current_run_params = RunParams(context_scope='task_local', task_attributes={'conversation_thread_id': parent.thread_id})
        missing = SelfMapReadContext.for_call(agent, ToolInvocationContext(agent.tools.runtime_snapshot()))
        assert missing.thread_id == ''
    finally:
        restore_current_subagent_context(agent, previous)


def test_permissions_keep_real_owner_mode_and_explicit_empty_roots(make_agent):
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
    agent = make_agent(kind='user')
    execute_approval_mode_operation(agent.home_paths, 'set', 'ask', authorization_host=agent)
    agent._current_run_params = RunParams(write_boundary={'read_scope_mode': 'exact',
        'allowed_read_roots': [], 'allowed_write_roots': [], 'network_enabled': False})
    current = _read(agent, 'permissions', item='current')['item']
    assert current['permissions']['mode'] == 'ask' and current['permissions']['source'] == 'owner'
    assert current['filesystem']['read_roots'] == [] and current['filesystem']['write_roots'] == []
    assert current['network']['enabled'] is False
    assert current['sandbox_observed'] == 'not_observed' and current['external_network'] == 'not_verified'


def test_directory_access_uses_executor_frozen_empty_roots_and_unbound_stays_unknown(make_agent):
    agent = make_agent()
    from agent_py_agent.agent.user_space.self_map_paths import _directory_item
    local = _read(agent, 'directories', item='workspace')['item']
    # owner根节点含受保护人格路径；原政策拒写必须保留false，未绑定的读范围仍未知。
    assert local['read_access'] is None and local['write_access'] is False
    assert local['denial_code'] == 'PERSONA_WRITE_REQUIRES_TOOL'
    assert local['access_state'] == 'not_observed'
    context = SelfMapReadContext.for_call(agent, ToolInvocationContext(agent.tools.runtime_snapshot()))
    ordinary = _directory_item('ordinary_business', agent.home_paths.owner_home_dir / 'business', context)
    assert ordinary['read_access'] is None and ordinary['write_access'] is None
    assert ordinary['access_state'] == 'not_observed' and ordinary['denial_code'] == ''
    scoped = execute_registry_test_call(agent.tools, 'self_map', {'section': 'directories', 'item': 'workspace'},
        write_boundary={'read_scope_mode': 'exact', 'allowed_read_roots': [], 'allowed_write_roots': []})
    assert scoped.ok, scoped.output
    current = json.loads(scoped.output)['item']
    assert current['read_access'] is False and current['write_access'] is False
    assert current['access_state'] == 'known'


def test_models_use_public_inventory_status_and_safe_modalities(make_agent, monkeypatch):
    from agent_py_agent.agent.settings.model_profiles import ModelProfileError
    agent = make_agent(model_input_modalities=['text', 'image'])
    agent.config.api_key = 'FAKE-MUST-NOT-LEAK'
    monkeypatch.setattr('agent_py_agent.agent.settings.model_profiles.public_shared_profiles',
                        lambda *_: (_ for _ in ()).throw(ModelProfileError('FAKE-PRIVATE-ERROR')))
    models = _read(agent, 'models')
    assert models['completeness'] == 'partial' and models['counts']['profiles'] is None
    current = next(row for row in models['items'] if row['id'] == 'current')
    assert current['input_modalities'] == ['text', 'image'] and current['endpoint_observed'] == 'not_verified'
    assert 'FAKE' not in json.dumps(models) and 'api_base' not in json.dumps(models)


def test_f4_reads_bound_configuration_only_and_never_exposes_locator(make_agent):
    agent = make_agent()
    calls = []
    agent.agents_source_repository = SimpleNamespace(
        configuration=lambda: calls.append('configuration') or SimpleNamespace(owner=agent.home_paths.owner_id,
            kind='local_file', revision=4, status='configured', failure_code='', locator='PRIVATE-LOCATOR'),
        load=lambda: pytest.fail('rule body loaded'), activation_metadata=lambda: pytest.fail('activation fetched'))
    item = _read(agent, 'memory', item='rules_source')['item']
    assert calls == ['configuration'] and item['revision'] == 4 and item['content_observed'] == 'not_observed'
    assert item['user_control'] == {'command': '/agents show', 'explicit_user_required': True}
    assert 'PRIVATE' not in json.dumps(item)
    agent.config.agents_source_enabled = False
    assert _read(agent, 'memory', item='rules_source')['item']['state'] == 'disabled'
    assert calls == ['configuration']


def test_f4_other_owner_configuration_never_exposes_metadata(make_agent):
    agent = make_agent()
    agent.agents_source_repository = SimpleNamespace(configuration=lambda: SimpleNamespace(
        owner='feishu/user/other', kind='PRIVATE-KIND', revision=123, status='configured', failure_code=''))
    item = _read(agent, 'memory', item='rules_source')['item']
    assert item['state'] == 'unavailable' and 'PRIVATE' not in json.dumps(item) and 'revision' not in item


def test_withdrawn_learned_skill_old_snapshot_is_not_declared_readable(make_agent, tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.skill_learning_maintenance import control_learning
    from agent_py_agent.tests.test_skill_learning import NAME, _output, _publish_first, _runtime
    ctx = _runtime(tmp_path / 'learned', _output())
    _publish_first(ctx)
    old = ctx.skills.snapshot_for(force_reload=True)
    entry = old.resolve('owner:' + NAME)
    agent = make_agent()
    agent._current_skill_snapshot = replace(old, owner_id=agent.home_paths.owner_id)
    control_learning(ctx.store, NAME, operation='disable')
    monkeypatch.setattr('agent_py_agent.agent.capability.skill_learning_versions.read_text_beneath',
                        lambda *a, **k: pytest.fail('body read'))
    item = _read(agent, 'skills', item=entry.stable_id)['item']
    assert item['state'] == 'revoked' and item['readable'] is False
    assert item['lifecycle']['state'] == 'disabled'
    assert item['next_read']['tool'] == 'self_map'


def test_group_discovery_keeps_credentials_and_monitor_authority_without_queries(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_group_shared_credentials import _group_case
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    ctx = _group_case(tmp_path)
    agent = ctx.agent
    before_calls, before_cards = list(ctx.calls), list(ctx.sent)
    outcome = execute_registry_test_call(agent.tools, 'self_map', {'section': 'tools', 'limit': 100},
        run_id=agent._current_run_params.run_id, register_with=agent,
        allowed_tools=['self_map', 'tool_search', 'request_credentials', 'schedule', 'watch_stream'])
    assert outcome.ok, outcome.output
    payload = json.loads(outcome.output)
    ids = {row['id'] for row in payload['items']}
    assert {'request_credentials', 'schedule', 'watch_stream'} <= ids
    assert not {'admin_controls', 'user_config'} & ids
    work = _read(agent, 'work')
    assert work['total'] is None
    assert ctx.calls == before_calls and ctx.sent == before_cards
    assert 'query_evidence' not in json.dumps(outcome.metadata)
    assert 'cref_' not in outcome.output
