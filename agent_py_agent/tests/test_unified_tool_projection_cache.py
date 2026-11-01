"""只缓存同一冻结授权快照的静态检索投影，不缓存执行许可或客户端租约。"""
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling.models import BaseTool, ToolModelHints
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolSearchTool
from agent_py_agent.tests._tool_runtime_harness import make_test_model_spec
from agent_py_agent.tests.test_tool_registry import registry

_FIXTURES = (registry,)


def _probe(registry, name='tenant_probe', keyword='amber'):
    spec = make_test_model_spec(name, description=keyword, category='testing')
    spec = replace(spec, hints=ToolModelHints(category='testing', default_deferred=True, deferred_summary='测试方法', keywords=(keyword,)))
    registry.register(type('Probe', (BaseTool,), {'model_spec': spec, 'runtime_policy': ToolSearchTool.runtime_policy})())


def _search(registry, snapshot, query='amber', category=''):
    params = {'query': query}
    if category:
        params['category'] = category
    return registry.tools['tool_search']._execute_snapshot(params, snapshot)


def _count_index(monkeypatch):
    calls, original = [], registry_module._tool_search_index_spec

    def counted(spec):
        calls.append(spec.name)
        return original(spec)

    monkeypatch.setattr(registry_module, '_tool_search_index_spec', counted)
    return calls


def test_one_frozen_snapshot_builds_projection_once_across_queries_and_categories(registry, monkeypatch):
    _probe(registry)
    snapshot = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'], run_id='run')
    calls = _count_index(monkeypatch)
    for category in ('', 'testing', '', 'testing'):
        result = _search(registry, snapshot, category=category)
        assert result.ok and result.result_envelope['tool_search']['loaded_tool_names'] == ['tenant_probe']
    assert [row.name for row in registry.search_deferred_specs('amber', runtime_snapshot=snapshot)] == ['tenant_probe']
    assert calls == ['tenant_probe']


def test_concurrent_searches_do_not_duplicate_same_projection(registry, monkeypatch):
    _probe(registry)
    snapshot = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'])
    calls = _count_index(monkeypatch)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: _search(registry, snapshot), range(12)))
    assert all(row.result_envelope['tool_search']['loaded_tool_names'] == ['tenant_probe'] for row in results)
    assert calls == ['tenant_probe']


def test_equal_schema_hashes_do_not_share_authorized_owner_or_space_views(registry, tmp_path):
    _probe(registry)
    own = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'], run_id='same-run')
    other = ToolRegistry(replace(registry._construction_params, workspace_root=tmp_path/'other',
                                owner_scope_root=str(tmp_path/'other'), operation_owner_id='local/other'))
    _probe(other, keyword='opal')
    theirs = other.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'], run_id='same-run')
    assert own.snapshot_hash == theirs.snapshot_hash
    assert _search(registry, own).result_envelope['tool_search']['loaded_tool_names'] == ['tenant_probe']
    assert _search(other, theirs).result_envelope['tool_search']['loaded_tool_names'] == []
    assert _search(other, theirs, 'opal').result_envelope['tool_search']['loaded_tool_names'] == ['tenant_probe']
    narrowed = registry.runtime_snapshot(allowed_tools=['tool_search'], run_id='same-run')
    assert _search(registry, narrowed).result_envelope['tool_search']['loaded_tool_names'] == []
    assert 'tenant_probe' not in _search(registry, narrowed, 'tenant_probe').result_envelope['tool_search']['loaded_tool_names']


def test_new_snapshot_sees_registration_disable_and_live_availability(registry, monkeypatch):
    _probe(registry)
    first = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'])
    assert _search(registry, first).result_envelope['tool_search']['loaded_tool_names'] == ['tenant_probe']
    _probe(registry, 'new_probe')
    second = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe', 'new_probe'])
    assert set(_search(registry, second).result_envelope['tool_search']['loaded_tool_names']) == {'tenant_probe', 'new_probe'}
    registry.disabled_tool_names.add('tenant_probe')
    from agent_py_agent.agent.tooling.models import ToolAvailability
    monkeypatch.setattr(registry.tools['new_probe'], 'availability', lambda: ToolAvailability(False, 'offline', 'TOOL_UNAVAILABLE'))
    revoked = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe', 'new_probe'])
    assert _search(registry, revoked).result_envelope['tool_search']['loaded_tool_names'] == []


def test_catalog_only_never_builds_or_loads_search_projection(registry, monkeypatch):
    _probe(registry)
    snapshot = registry.runtime_snapshot(allowed_tools=['list_tools', 'tenant_probe'])
    calls = _count_index(monkeypatch)
    result = _search(registry, snapshot)
    assert not result.ok and result.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert calls == [] and result.result_envelope.get('tool_search') is None


def test_projection_cache_is_bounded_and_does_not_keep_snapshot_alive(registry):
    _probe(registry)
    snapshots = []
    for index in range(24):
        snapshot = registry.runtime_snapshot(allowed_tools=['tool_search', 'tenant_probe'], run_id=f'run-{index}')
        assert _search(registry, snapshot).ok
        snapshots.append(snapshot)
    cache = getattr(registry, '_deferred_projection_cache', None)
    assert cache is not None and 0 < len(cache) <= 8
    reference = weakref.ref(snapshot)
    snapshots.clear()
    del snapshot
    gc.collect()
    assert reference() is None
