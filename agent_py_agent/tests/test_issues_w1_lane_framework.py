"""W1通道合同：真实临时home与真实调度器，领域函数用独立stub提供。"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import FrozenInstanceError, fields
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import home_retention, owner_maintenance, storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


# 函数用途: 令尚未合入的全部领域入口明确走原保留回执。
def _missing_lanes(monkeypatch):
    monkeypatch.setattr(storage_lanes, 'LANE_BINDINGS', dict.fromkeys(
        storage_lanes.LANE_BINDINGS, 'nonexistent_issue_lane_module:x'))


# 函数用途: 在真实解析路径注入一个仅供本例使用的领域模块。
def _bind_lane(monkeypatch, lane, kind='compact_checkpoints'):
    module = ModuleType('agent_py_agent.agent.issues_lane_stub')
    module.maintain = lane
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(storage_lanes.LANE_BINDINGS, kind, 'issues_lane_stub:maintain')
    return kind


# 函数用途: 构造入口实际消费的最小保留报告，不替换分派或配额实现。
def _policy(*, legal_hold=False, applied=True, load_errors=()):
    return SimpleNamespace(legal_hold=legal_hold, applied=applied, load_errors=load_errors)


# 函数用途: 在真实原入口运行一片足够覆盖空home的维护。
def _run(home, previous=None, retention=None, **options):
    return owner_maintenance.run_owner_storage_maintenance_slice(
        home, previous or {}, retention or _policy(), now=2_000_000,
        quota_max_ms=1000, quota_max_entries=4096, **options)


# 函数用途: 创建真实隔离home，并明确所有领域通道初始缺失。
@pytest.fixture
def lane_home(tmp_path, monkeypatch):
    _missing_lanes(monkeypatch)
    return ensure_my_agent_home(tmp_path / 'home')


# 函数用途: 固定前32项的顺序与新增四类的元数据，不把登记当作删除许可。
def test_registry_appends_four_kinds_without_changing_original_order():
    original = ('long_term', 'candidates', 'memory_operations', 'vector_cache', 'raw_memory_audit', 'daily_curator',
        'conversation_messages', 'compact_checkpoints', 'context_bundles', 'tool_outputs', 'local_records', 'local_events',
        'runtime_events', 'scheduler_history', 'global_indexes', 'gateway_active', 'gateway_terminal', 'gateway_logs',
        'security_audit', 'skill_usage', 'skill_registry', 'installed_versions', 'task_workspaces', 'subagents',
        'capability_grants', 'media_inputs', 'persona_versions', 'outbox_delivery', 'process_sessions', 'cache_tmp_trash',
        'legacy_layout', 'session_pair_rate')
    registry = home_retention.storage_kind_registry()
    assert len(registry) == len({kind.kind for kind in registry}) == 36
    assert tuple(kind.kind for kind in registry[:32]) == original
    assert tuple(kind.kind for kind in registry[32:]) == (
        'display_archives', 'conversation_ledgers', 'asset_auxiliary', 'session_task_payloads')
    for kind in registry[32:]:
        assert kind.scope and kind.authority and kind.policy_ref and kind.protected_refs and kind.actions
        with pytest.raises(LookupError):
            kind.resolve_paths({})


# 函数用途: 固定DTO字段次序、不可变性、预算封顶和原写门返回值。
def test_lane_dto_budget_and_run_write(monkeypatch, lane_home):
    assert [field.name for field in fields(storage_lanes.StorageLaneBudget)] == ['max_items', 'max_bytes', 'deadline']
    assert [field.name for field in fields(storage_lanes.StorageLaneRequest)] == [
        'kind', 'home', 'now', 'cursor', 'budget', 'legal_hold', 'coordinator', 'pressure', 'commit']
    monkeypatch.setattr(storage_lanes.time, 'monotonic', lambda: 100.0)
    unlimited = storage_lanes.StorageLaneBudget(None, None, None)
    assert unlimited.remaining_ms() == math.inf and not unlimited.exhausted()
    budget = storage_lanes.StorageLaneBudget(4, 32, 100.02)
    assert budget.remaining_ms() == pytest.approx(20)
    assert budget.exhausted(items=4) and budget.exhausted(size=32) and not budget.exhausted(items=3, size=31)
    capped = budget.capped(items=2, size=64, ms=5)
    assert (capped.max_items, capped.max_bytes) == (2, 32)
    assert capped.deadline == pytest.approx(100.005)
    assert storage_lanes.StorageLaneBudget(0, 0, 99).exhausted()
    with pytest.raises(FrozenInstanceError):
        budget.max_items = 8
    calls = []

    # 函数用途: 记录真实operation被写门消费且返回同一结果。
    def commit(operation):
        calls.append(operation)
        return operation()

    value = object()
    request = storage_lanes.StorageLaneRequest('compact_checkpoints', lane_home, 1.0, {}, budget, False, True, None, commit)
    assert calls == []
    assert request.run_write(lambda: value) is value and len(calls) == 1
    direct = storage_lanes.StorageLaneRequest('compact_checkpoints', lane_home, 1.0, {}, budget, False, False, None, None)
    assert direct.run_write(lambda: value) is value


# 函数用途: 缺失的十个通道均有保留回执，不制造未绑定类别或永久续作。
def test_all_missing_lanes_fall_back_and_finish(lane_home):
    state = _run(lane_home)
    for _ in range(8):
        if not state['continuation_pending']:
            break
        state = _run(lane_home, {'storage_maintenance': state})
    assert state['unbound_kinds'] == [] and not state['continuation_pending']
    assert state['lane_fallback_kinds'] == sorted(storage_lanes.LANE_BINDINGS)
    assert len(state['lane_fallback_kinds']) == 10 and state['lane_reports'] == {}


# 函数用途: 验证单进程DTO、hold透传和真实调度器发布的游标与可选计量。
@pytest.mark.parametrize('legal_hold', [False, True])
def test_bound_lane_receives_request_and_publishes_validated_cursor(lane_home, monkeypatch, legal_hold):
    received = []

    # 函数用途: 返回已确认的只读进度，不产生写入或物理释放声明。
    def lane(request):
        received.append(request)
        return {'cursor': {'after': 'confirmed'}, 'complete': True, 'items': 1, 'bytes': 3,
                'protected': {'active': 2}, 'skipped': {'lock_busy': 1}, 'backlog': 2}

    kind = _bind_lane(monkeypatch, lane)
    state = _run(lane_home, retention=_policy(legal_hold=legal_hold))
    assert len(received) == 1
    request = received[0]
    assert isinstance(request, storage_lanes.StorageLaneRequest) and request.home is lane_home
    assert request.coordinator is False and request.commit is None and request.legal_hold is legal_hold
    assert state['cursors'][kind] == {'after': 'confirmed'}
    assert state['lane_reports'][kind]['protected'] == {'active': 2}
    assert 'cursor' not in state['lane_reports'][kind]


# 函数用途: 非法回执即使改过输入副本，也不能覆盖上一片已提交游标。
@pytest.mark.parametrize('invalid', ['oversized', 'unknown'])
def test_invalid_receipt_keeps_previous_cursor(lane_home, monkeypatch, invalid):
    # 函数用途: 模拟未通过校验的领域返回，不替换dispatcher。
    def lane(request):
        request.cursor['after'] = 'uncommitted'
        receipt = {'cursor': {'data': 'x' * 20_480}, 'complete': True, 'items': 1, 'bytes': 0}
        if invalid == 'unknown':
            receipt = {**receipt, 'cursor': {}, 'unexpected': True}
        return receipt

    kind = _bind_lane(monkeypatch, lane)
    previous = {'storage_maintenance': {'continuation_pending': True, 'pending_kinds': [kind],
                                      'cursors': {kind: {'after': 'confirmed'}}}}
    state = _run(lane_home, previous)
    assert state['cursors'][kind] == previous['storage_maintenance']['cursors'][kind] == {'after': 'confirmed'}
    assert state['errors'][0]['kind'] == kind and state['errors'][0]['error_type'] == 'ValueError'
    assert kind not in state['lane_reports'] and state['continuation_pending']


# 函数用途: 未完成的lane保留准确pending及下一拍续作责任。
def test_incomplete_lane_keeps_continuation(lane_home, monkeypatch):
    kind = _bind_lane(monkeypatch, lambda request: {
        'cursor': {'after': 'page-1'}, 'complete': False, 'items': 1, 'bytes': 0})
    state = _run(lane_home)
    assert kind in state['pending_kinds'] and state['continuation_pending']
    assert state['cursors'][kind] == {'after': 'page-1'}


# 函数用途: 校验回执的拒绝矩阵，坏类型不被Python的bool/int兼容关系放行。
@pytest.mark.parametrize('patch', [
    {'cursor': []}, {'cursor': {'x': {1}}}, {'cursor': {'x': float('nan')}},
    {'complete': 1}, {'items': True}, {'items': -1}, {'bytes': 1.5},
    {'freed_bytes': -1}, {'deleted_items': False}, {'backlog': -1},
    {'protected': True}, {'protected': {'active': False}}, {'protected': {'active': -1}},
    {'skipped': []}, {'skipped': {1: 2}}, {'skipped': {'lock_busy': -1}},
    {'errors': [{'code': 'read'}] * 17}, {'errors': [{}]}, {'errors': [{'code': 1}]},
    {'errors': [{'code': 'read', 'path': '/outside'}]}, {'errors': [{'code': 'read', 'path': '../outside'}]},
    {'oldest_due_age_seconds': float('nan')}, {'oldest_due_age_seconds': float('inf')},
    {'oldest_due_age_seconds': True}, {'oldest_due_age_seconds': -1}, {'deferred_reason': 3}, {'extra': 0},
])
def test_receipt_rejects_invalid_fields(patch):
    with pytest.raises(ValueError, match='^invalid_storage_lane_receipt$'):
        storage_lanes.validate_lane_receipt({'cursor': {}, 'complete': True, 'items': 0, 'bytes': 0, **patch})


# 函数用途: 必需字段逐个核验，校验成功返回浅副本并保留两种protected计量。
@pytest.mark.parametrize('missing', ['cursor', 'complete', 'items', 'bytes'])
def test_receipt_requires_all_fields(missing):
    receipt = {'cursor': {}, 'complete': True, 'items': 0, 'bytes': 0}
    del receipt[missing]
    with pytest.raises(ValueError, match='invalid_storage_lane_receipt'):
        storage_lanes.validate_lane_receipt(receipt)


# 函数用途: 以实际JSON编码长度验证16KiB边界，允许值的副本不变更来源。
def test_receipt_cursor_exact_boundary_and_shallow_copy():
    overhead = len(json.dumps({'data': ''}, separators=(',', ':'), sort_keys=True))
    cursor = {'data': 'x' * (storage_lanes.STORAGE_LANE_CURSOR_MAX_BYTES - overhead)}
    for protected in (3, {'active': 3}):
        receipt = {'cursor': cursor, 'complete': True, 'items': 1, 'bytes': 0, 'protected': protected,
                   'errors': [{'code': 'read', 'path': 'conversations/a'}], 'oldest_due_age_seconds': 1.5}
        checked = storage_lanes.validate_lane_receipt(receipt)
        assert checked == receipt and checked is not receipt and checked['cursor'] is cursor
    with pytest.raises(ValueError, match='invalid_storage_lane_receipt'):
        storage_lanes.validate_lane_receipt({'cursor': {'data': cursor['data'] + 'x'},
                                            'complete': True, 'items': 0, 'bytes': 0})
    with pytest.raises(ValueError, match='invalid_storage_lane_receipt'):
        storage_lanes.validate_lane_receipt(None)


# 函数用途: 原保留覆盖项缺失或非法时沿用默认值，绝不回写策略。
@pytest.mark.parametrize('value,expected', [(None, 7), (True, 7), (0, 7), (-1, 7), (3651, 7),
                                          (1.5, 7), ('3', 7), (3, 3), (3650, 3650)])
def test_retention_override_is_strict_and_read_only(lane_home, value, expected):
    path = lane_home.owner_retention_json
    payload = json.loads(path.read_text())
    if value is not None:
        payload['lane_custom_days'] = value
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    assert storage_lanes.owner_retention_override(lane_home, 'lane_custom_days', 7) == expected
    assert path.read_bytes() == before


# 函数用途: 只发现本owner真实且排序后的会话根，scope链接不能引入外置根。
def test_conversation_roots_are_sorted_and_skip_scope_symlink(lane_home, tmp_path):
    direct = lane_home.owner_home_dir / 'conversations'
    (direct / 'threads').mkdir(parents=True, exist_ok=True)
    runtime = lane_home.owner_workspace_dir / 'runtime' / 'workspaces'
    roots = [runtime / scope / 'conversations' for scope in ('z', 'a')]
    for path in roots:
        path.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / 'outside'
    (outside / 'conversations').mkdir(parents=True)
    (runtime / 'linked').symlink_to(outside, target_is_directory=True)
    assert storage_lanes.owner_conversation_roots(lane_home) == (direct, roots[1], roots[0])
    missing = SimpleNamespace(owner_home_dir=tmp_path / 'missing', owner_workspace_dir=tmp_path / 'missing' / 'workspace')
    assert storage_lanes.owner_conversation_roots(missing) == ()
    assert not missing.owner_home_dir.exists()


# 函数用途: 对流式名称只保留limit个最小后继，不要求可重复迭代或全量排序。
def test_lane_cursor_window_streams_once_and_bounds_results():
    seen = []

    # 函数用途: 提供只消费一次的逆序名称来源。
    def names():
        for value in range(100, 0, -1):
            seen.append(value)
            yield f'{value:03}'

    assert storage_lanes.lane_cursor_window({'after': '050'}, names(), 3) == ['051', '052', '053']
    assert len(seen) == 100
    assert storage_lanes.lane_cursor_window({}, iter(['b', 'a']), 0) == []


# 函数用途: 复用原数据库发现器而不构造新库或建立循环导入。
def test_local_database_helper_delegates_to_original_discovery(lane_home, monkeypatch):
    sentinel = ([lane_home.owner_data_dir / 'local.db'], [{'source': 'existing', 'error_type': 'OSError'}])
    calls = []

    # 函数用途: 仅观察helper调用的既有领域发现接口。
    def discover(root, *, include_event_files=False):
        calls.append((root, include_event_files))
        return sentinel

    monkeypatch.setattr(owner_maintenance, '_local_store_database_paths', discover)
    assert storage_lanes.owner_local_store_databases(lane_home) is sentinel
    assert calls == [(lane_home.owner_home_dir, True)]


# 函数用途: 目标缺失才回退，已有领域的内部依赖或加载异常须保留为错误。
@pytest.mark.parametrize('error', [ImportError('dependency unavailable'),
    ModuleNotFoundError('dependency unavailable', name='issues_unavailable_dependency'), ValueError('invalid lane module')])
def test_lane_resolver_propagates_internal_import_errors(lane_home, monkeypatch, error):
    kind = _bind_lane(monkeypatch, lambda request: {})
    original = storage_lanes.importlib.import_module

    # 函数用途: 模拟已有stub模块加载内部依赖失败，保留原异常身份。
    def load(name):
        if name == 'agent_py_agent.agent.issues_lane_stub':
            raise error
        return original(name)

    monkeypatch.setattr(storage_lanes.importlib, 'import_module', load)
    with pytest.raises(type(error)) as raised:
        storage_lanes.resolve_storage_lane(kind)
    assert raised.value is error
    assert storage_lanes.resolve_storage_lane('not-registered') is None


# 函数用途: 缺少入口属性只回退，不将缺失函数误报为内部依赖失败。
def test_lane_resolver_missing_attribute_falls_back(lane_home, monkeypatch):
    kind = _bind_lane(monkeypatch, lambda request: {})
    monkeypatch.setitem(storage_lanes.LANE_BINDINGS, kind, 'issues_lane_stub:absent')
    assert storage_lanes.resolve_storage_lane(kind) is None
