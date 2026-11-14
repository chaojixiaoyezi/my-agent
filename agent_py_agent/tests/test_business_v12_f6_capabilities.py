"""W1冻结端口fake配合原路由、快照和使用账；不把局部组合写成真实W2/W13端到端。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.business_capabilities import (
    BusinessCapabilityRequest,
    BusinessCapabilityService,
    resolve_business_capabilities,
)
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability.router import CapabilityRouter
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.capability.skill_service import SkillsService
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.tests.test_mega_business_api_actions import native_http
from agent_py_agent.tests.test_tool_registry import registry

_FIXTURES = (registry, native_http)


def _package(name, letter):
    def reject_body(_path):
        raise AssertionError('选择能力不得预读包正文')
    return CapabilityPackageSnapshot(package_id=name, installation_scope='private', version='1', summary='batch',
                                     description='batch processing', keywords=('batch',), entry_document='SKILL.md',
                                     package_sha256=letter*64, activation_id='f'*64,
                                     members=(CapabilityFile('SKILL.md', letter*64),), reader=reject_body)


def _host(tmp_path, registry):
    base = ensure_my_agent_home(tmp_path/'home')
    home = home_paths_with_owner(base, ensure_owner_home(base.root, OwnerIdentity.local_main()))
    packages = [_package('mounted', 'a'), _package('batch', 'b')]
    skills = SkillsService(home_paths=home, workspace_root=tmp_path/'workspace',
                           policy_provider=lambda: resolve_effective_owner_policy(home), package_provider=lambda: tuple(packages))
    context = BusinessExecutionContext(home.owner_id, 'space-a', None, 'stask', 1, None, (), (), 'business-context.v1')
    actor = object()

    class SpacePort:
        def __init__(self):
            self.revoked = False
            self.mounts = [{'package_ref': packages[0].stable_id, 'enabled': True, 'position': 0,
                            'version_ref': None, 'availability': {'ref': packages[0].stable_id, 'state': 'available',
                                'reason_code': '', 'required_capabilities': []}}]

        def get(self, space_id, *, actor):
            if actor is not host.business_actor or self.revoked:
                raise PermissionError('scope revoked')
            mounts = self.mounts if space_id == 'space-a' else []
            return {'space': {'space_id': space_id, 'state': 'active', 'revision': 2, 'mounts': mounts},
                    'mounts': mounts, 'interface_refs': [], 'policy_refs': [], 'allowed_actions': []}

    class Host:
        def current_skill_snapshot(self):
            return self.frozen_skills

    host = Host()
    host.config = SimpleNamespace(business_spaces_enabled=True)
    host.home_paths, host.business_actor = home, actor
    host.skills_service, host.tools = skills, registry
    host.frozen_skills = skills.snapshot_for()
    host._current_run_params = SimpleNamespace(run_id='run', tool_runtime_snapshot=registry.runtime_snapshot(run_id='run'),
                                               task_attributes={'business_context': context.to_dict()})
    host.business_spaces = SpacePort()
    host.capability_router = CapabilityRouter(skill_snapshot_provider=host.current_skill_snapshot,
                                              tool_specs=list(host._current_run_params.tool_runtime_snapshot.specs))
    return host, context, packages


def test_real_router_prioritizes_matching_mount_without_loading_or_granting(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    before = host._current_run_params.tool_runtime_snapshot
    hits = host.capability_router.search('batch', limit=10, kinds={'capability_package'})
    assert hits[0].card.metadata['stable_id'] == packages[0].stable_id
    resolved = resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('batch'), host=host)
    assert resolved.candidates[0]['reason_codes'] == ['query_match', 'mounted_first']
    assert host._current_run_params.tool_runtime_snapshot is before


def test_missing_disabled_version_mismatch_and_irrelevant_mount_keep_original_route(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    for change in ({'enabled': False}, {'version_ref': 'wrong-version'}, {'package_ref': 'capability:missing'}):
        original = dict(host.business_spaces.mounts[0])
        host.business_spaces.mounts[0].update(change)
        resolved = resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('batch'), host=host)
        assert resolved.hits[0].card.metadata['stable_id'] == packages[1].stable_id
        assert resolved.unavailable
        host.business_spaces.mounts[0] = original
    # 单个无匹配词；“query”本身会合法命中原生查询工具，不能用它断言无候选。
    unrelated = resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('zxqvbnm912'), host=host)
    assert unrelated.candidates == ()


def test_live_package_and_space_revocation_do_not_reuse_old_authorization(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    assert resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('batch'), host=host).hits
    removed = packages.pop(0)
    resolved = resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('batch'), host=host)
    assert removed.stable_id not in {row['method_ref'] for row in resolved.candidates}
    host.business_spaces.revoked = True
    with pytest.raises(PermissionError):
        resolve_business_capabilities(context=context, request=BusinessCapabilityRequest('batch'), host=host)


def test_owner_space_and_snapshot_scope_are_not_taken_from_request_prose(registry, tmp_path):
    host, context, packages = _host(tmp_path, registry)
    with pytest.raises(PermissionError):
        resolve_business_capabilities(context=replace(context, owner_id='local/other'), request=BusinessCapabilityRequest('batch'), host=host)
    other_space = replace(context, space_id='space-b')
    resolved = resolve_business_capabilities(context=other_space, request=BusinessCapabilityRequest('batch'), host=host)
    assert resolved.candidates[0]['method_ref'] == packages[1].stable_id
    assert 'mounted_first' not in resolved.candidates[0]['reason_codes']
    host.config.business_spaces_enabled = False
    hits = host.capability_router.search('batch', limit=10, kinds={'capability_package'})
    assert hits[0].card.metadata['stable_id'] == packages[1].stable_id


def test_read_actions_share_resolution_and_report_unknown_usage_without_source(registry, tmp_path):
    host, context, _ = _host(tmp_path, registry)
    service = BusinessCapabilityService(host)
    resolved = service.execute('spaces.capabilities.resolve', {'space_id': 'space-a', 'query': 'batch'}, context=context)
    assert set(resolved) == {'candidates', 'origins', 'unavailable', 'policy_revision'}
    assert resolved['candidates'][0]['reason_codes'] == ['query_match', 'mounted_first']
    assert business_action_spec('spaces.capabilities.resolve').validate_result(resolved) == resolved
    usage = service.execute('spaces.usage.list', {'space_id': 'space-a'}, context=context)
    assert usage == {'items': [], 'next_cursor': None, 'coverage': 'unknown'}
    assert business_action_spec('spaces.usage.list').validate_result(usage) == usage
    with pytest.raises(PermissionError):
        service.execute('spaces.capabilities.resolve', {'space_id': 'space-b'}, context=context)


def test_usage_projects_original_ledger_only_with_host_task_space_binding(registry, tmp_path):
    host, context, _ = _host(tmp_path, registry)
    store = SkillLearningStore.for_home(host.home_paths)
    store.record_skill_usage(event='manual_get', skill_id='owner:method', source='skill_search', task_id='runtime-task')
    host.task_business_context = lambda task: context if task == 'runtime-task' else None
    service = BusinessCapabilityService(host)
    usage = service.execute('spaces.usage.list', {'space_id': 'space-a'}, context=context)
    assert usage['coverage'] == 'partial'
    assert [(row['method_ref'], row['phase'], row['task_id']) for row in usage['items']] == [('owner:method', 'read', 'stask')]
    assert usage['items'][0]['run_id'] is None
    assert business_action_spec('spaces.usage.list').validate_result(usage) == usage
    other_space = replace(context, space_id='space-b')
    assert service.execute('spaces.usage.list', {'space_id': 'space-b'}, context=other_space)['items'] == []


def test_package_catalog_uses_original_install_records_and_scope_bound_cursor(registry, tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import business_capabilities as business
    host, context, _ = _host(tmp_path, registry)
    records = [{'package_id': str(index), 'name': str(index), 'summary': 'batch', 'digest': 'a'*64, 'enabled': True}
               for index in range(3)]
    monkeypatch.setattr(business, 'list_installed_capability_packages', lambda owner: records, raising=False)
    service = BusinessCapabilityService(host)
    first = service.execute('spaces.packages.catalog', {'limit': 2})
    assert [row['package_ref'] for row in first.get('items', [])] == ['capability:0', 'capability:1']
    assert first['next_cursor']
    assert business_action_spec('spaces.packages.catalog').validate_result(first) == first
    second = service.execute('spaces.packages.catalog', {'limit': 2, 'cursor': first['next_cursor']})
    assert [row['package_ref'] for row in second['items']] == ['capability:2'] and second['next_cursor'] is None
    records[0]['enabled'] = False
    from agent_py_agent.agent.conversation.business_access import BusinessError
    with pytest.raises(BusinessError) as caught:
        service.execute('spaces.packages.catalog', {'cursor': first['next_cursor']})
    assert caught.value.code == 'CURSOR_EXPIRED'
    assert caught.value.details['reason'] == 'CURSOR_SCOPE_OR_SOURCE_CHANGED'


def test_catalog_cursor_errors_roundtrip_without_becoming_host_outage(native_http, monkeypatch):
    import json

    from agent_py_agent.agent.capability import business_capabilities as business
    from agent_py_agent.cli.space_commands import BusinessClient
    from agent_py_agent.tests.test_mega_business_api_actions import native_request

    records = [{'package_id': str(index), 'name': str(index), 'summary': 'batch',
        'digest': 'a'*64, 'enabled': True} for index in range(3)]
    monkeypatch.setattr(business, 'list_installed_capability_packages', lambda owner: records)

    def transport(path, body, timeout):
        status, _, raw = native_request(native_http, 'POST', path, body, user='local-agent',
            headers={'X-Channel': 'chat', 'X-Conversation-Id': 'catalog-cursor'})
        return status, json.loads(raw)

    client = BusinessClient(transport)
    first = client.call('spaces.packages.catalog', {'limit': 2, 'query': 'batch'})
    assert first['ok'], first
    cursor = first['data']['next_cursor']
    for params, reason in [({'cursor': cursor}, 'CURSOR_SCOPE_OR_SOURCE_CHANGED'),
                           ({'cursor': 'not-a-private-cursor!'}, 'CURSOR_FORMAT_INVALID')]:
        rejected = client.call('spaces.packages.catalog', params)
        assert rejected['http_status'] == 410 and rejected['error']['code'] == 'CURSOR_EXPIRED', rejected
        assert rejected['effect_outcome'] == 'not_started'
        details = {item['key']: item['value'] for item in rejected['error']['details']}
        assert details['reason'] == reason and details['field'] == 'cursor'
        assert 'query' in details['pagination_recovery'] and '1至100' in details['pagination_recovery']
        assert cursor not in json.dumps(rejected) and 'not-a-private-cursor!' not in json.dumps(rejected)
    last = client.call('spaces.packages.catalog', {'query': 'batch', 'limit': 1, 'cursor': cursor})
    assert last['ok'] and [row['package_ref'] for row in last['data']['items']] == ['capability:2']
    assert last['data']['next_cursor'] is None

    def source_unavailable(owner):
        raise OSError('synthetic unavailable source')

    monkeypatch.setattr(business, 'list_installed_capability_packages', source_unavailable)
    unavailable = client.call('spaces.packages.catalog', {'query': 'batch', 'cursor': cursor})
    assert unavailable['http_status'] == 503 and unavailable['error']['code'] == 'HOST_NOT_READY'
    assert unavailable['effect_outcome'] == 'not_started'


def test_fresh_model_visibility_cannot_be_reauthorized_by_old_frozen_view(registry, tmp_path, monkeypatch):
    host, context, _ = _host(tmp_path, registry)
    request = BusinessCapabilityRequest('audit_group_query')
    before = resolve_business_capabilities(context=context, request=request, host=host)
    assert 'tool:audit_group_query' in {item['method_ref'] for item in before.candidates}
    original = registry.runtime_snapshot

    def hidden_snapshot(**kwargs):
        snapshot = original(**kwargs)
        return replace(snapshot, snapshot_hash='', runtimes=tuple(
            replace(row, exposure=replace(row.exposure, model_visible=False))
            if row.model_spec.name == 'audit_group_query' else row for row in snapshot.runtimes))

    monkeypatch.setattr(registry, 'runtime_snapshot', hidden_snapshot)
    after = resolve_business_capabilities(context=context, request=request, host=host)
    assert 'tool:audit_group_query' not in {item['method_ref'] for item in after.candidates}


def test_internal_long_recommendation_preserves_business_scope_and_external_limit(registry, tmp_path):
    host, _context, _packages = _host(tmp_path, registry)
    query = 'batch ' + '长运行器上下文' * 2000
    rendered = host.capability_router.render_package_recommendations(query, limit=5)
    assert 'mounted' in rendered
    with pytest.raises(ValueError, match='CAPABILITY_QUERY_TOO_LONG'):
        host.capability_router.search(query, limit=5, kinds={'capability_package'})
    with pytest.raises(ValueError, match='CAPABILITY_QUERY_TOO_LONG'):
        BusinessCapabilityRequest('x' * 8001)
    host.business_spaces.revoked = True
    with pytest.raises(PermissionError, match='scope revoked'):
        host.capability_router.render_package_recommendations(query, limit=5)


@pytest.mark.parametrize(('query', 'limit', 'code'), [
    (None, 1, 'CAPABILITY_QUERY_TYPE_INVALID'),
    ('x' * 8001, 1, 'CAPABILITY_QUERY_TOO_LONG'),
    ('batch', True, 'CAPABILITY_LIMIT_TYPE_INVALID'),
    ('batch', -1, 'CAPABILITY_LIMIT_NEGATIVE'),
])
def test_capability_request_explains_invalid_input(query, limit, code):
    with pytest.raises(ValueError, match=code) as caught:
        BusinessCapabilityRequest(query, limit)
    assert '尚未执行' in str(caught.value)
