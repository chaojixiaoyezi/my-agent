"""W1协调者通道、原写门、压力顺序与预算扫描兼容。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.user_space import (
    home_retention,
    owner_maintenance,
    owner_maintenance_transition,
    owner_quota,
    storage_lanes,
)
from agent_py_agent.tests.test_issues_w1_lane_framework import (
    _bind_lane,
    _missing_lanes,
    _policy,
    _run,
)
from agent_py_agent.tests.test_issues_w1_lane_framework import (
    lane_home as lane_home,
)


# 函数用途: 只增加预算扫描报告字段，原保留报告的动作、统计和序列化保持原样。
class _ScanReport:
    # 函数用途: 保存领域服务的真实报告和本拍是否仍有扫描欠账。
    def __init__(self, report, partial):
        self.report, self.scan_partial = report, partial

    # 函数用途: 保持原报告的完整读取接口。
    def __getattr__(self, name):
        return getattr(self.report, name)


# 函数用途: 原配额提交后lane只通过同一个commit追加一次写入，游标仍归调度器发布。
def test_coordinator_lane_uses_original_commit_and_preserves_legacy_fields(lane_home, monkeypatch):
    commits, received = [], []

    # 函数用途: 记录并实际执行交付的operation，不替换维护dispatcher。
    def commit(operation):
        commits.append(operation)
        return operation()

    # 函数用途: 在注入的领域入口中用原request写门执行一个真实临时变更。
    def lane(request):
        received.append(request)
        assert request.run_write(lambda: 'committed-result') == 'committed-result'
        return {'cursor': {'after': 'coordinator-confirmed'}, 'complete': True, 'items': 1, 'bytes': 0}

    kind = _bind_lane(monkeypatch, lane)
    state = _run(lane_home, coordinator=True, commit_transition=commit)
    assert len(commits) == 2 and len(received) == 1
    assert received[0].coordinator is True and received[0].commit is commit
    assert state['cursors'][kind] == {'after': 'coordinator-confirmed'}
    assert state['coordinator_lanes']['ran'] == [kind]
    assert state['deferred_reason'] == 'coordinator_adapter_protocol_required' and state['deletion_enabled'] is False


# 函数用途: 空所有者的所有缺失通道均只读回退，原配额仍是唯一提交。
def test_coordinator_empty_owner_all_fallbacks_make_zero_lane_commits(lane_home):
    commits = []

    # 函数用途: 计数原配额与领域写门的实际调用。
    def commit(operation):
        commits.append(operation)
        return operation()

    state = _run(lane_home, coordinator=True, commit_transition=commit)
    assert len(commits) == 1
    assert state['coordinator_lanes']['ran'] == []
    assert state['coordinator_lanes']['fallback_kinds'] == sorted(storage_lanes.LANE_BINDINGS)
    assert not state['coordinator_lanes']['errors'] and not state['coordinator_lanes']['pending_kinds']


# 函数用途: 没有原写门时不运行任何lane，旧延期语义和状态头保留。
def test_coordinator_without_commit_never_enters_lane(lane_home, monkeypatch):
    _bind_lane(monkeypatch, lambda request: pytest.fail('lane entered without coordinator commit'))
    state = _run(lane_home, coordinator=True)
    assert 'coordinator_lanes' not in state
    assert state['deferred_reason'] == 'coordinator_adapter_protocol_required'
    assert state['deletion_enabled'] is False and state['continuation_pending'] is False
    assert state['unbound_kinds'] == [kind.kind for kind in home_retention.storage_kind_registry()]


# 函数用途: 保留不安全时不进入lane，原配额动作仍按既有合同处理。
@pytest.mark.parametrize('report', [_policy(applied=False), _policy(load_errors=['unavailable'])])
def test_coordinator_unsafe_retention_never_enters_lane(lane_home, monkeypatch, report):
    _bind_lane(monkeypatch, lambda request: pytest.fail('unsafe retention entered lane'))
    state = _run(lane_home, retention=report, coordinator=True, commit_transition=lambda operation: operation())
    assert 'coordinator_lanes' not in state


# 函数用途: 协调者的未完lane也保留pending责任而非等到下个日周期。
def test_coordinator_incomplete_lane_retains_continuation(lane_home, monkeypatch):
    kind = _bind_lane(monkeypatch, lambda request: {
        'cursor': {'after': 'page-1'}, 'complete': False, 'items': 1, 'bytes': 0})
    state = _run(lane_home, coordinator=True, commit_transition=lambda operation: operation())
    assert kind in state['coordinator_lanes']['pending_kinds'] and state['continuation_pending']


# 函数用途: 两个原入口对旧/新保留签名均可运行，源扫描partial必须真正再次驱动保留服务。
@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('supports_budget', [False, True])
def test_scan_budget_compatibility_and_partial_resumes_real_retention(lane_home, monkeypatch, coordinator, supports_budget):
    module = owner_maintenance_transition if coordinator else owner_maintenance
    original = module.apply_owner_retention
    seen = []

    if supports_budget:
        # 函数用途: 模拟W2合入后的明确关键字合同，内部仍使用当前真实保留实现。
        def apply(home, *, now=None, commit_transition=None, expected_policy_fingerprint=None, scan_budget=False):
            seen.append(scan_budget)
            return _ScanReport(original(home, now=now, commit_transition=commit_transition,
                expected_policy_fingerprint=expected_policy_fingerprint), len(seen) == 1)
    else:
        # 函数用途: 保留W2合入前的精确旧签名，不接受多余参数以验证兼容调用。
        def apply(home, *, now=None, commit_transition=None, expected_policy_fingerprint=None):
            seen.append(False)
            return _ScanReport(original(home, now=now, commit_transition=commit_transition,
                expected_policy_fingerprint=expected_policy_fingerprint), len(seen) == 1)

    monkeypatch.setattr(module, 'apply_owner_retention', apply)
    options = {'include_global_indexes': False}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    first = owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options)
    assert first.ran and seen == [supports_budget]
    path = lane_home.owner_data_dir / 'maintenance.json'
    marker = json.loads(path.read_text())
    assert marker['retention_scan_partial'] is True and marker['maintenance_partial'] is True
    assert owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_001)
    second = owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_001, **options)
    assert second.ran and seen == [supports_budget, supports_budget]
    assert json.loads(path.read_text())['retention_scan_partial'] is False


# 函数用途: 高水位只改变两入口的调度次序及信息快照，不对策略文件做修改。
@pytest.mark.parametrize('coordinator', [False, True])
def test_pressure_prioritizes_display_archives_without_policy_changes(lane_home, monkeypatch, coordinator):
    calls = []
    before = lane_home.owner_retention_json.read_bytes()
    pressure = {'level': 'high', 'used_bytes': 90, 'max_bytes': 100, 'ratio': 0.9, 'authoritative': False, 'reason': 'sample'}
    monkeypatch.setattr(owner_quota, 'owner_quota_pressure', lambda root: pressure, raising=False)

    # 函数用途: 返回只读回执，观察真实分派顺序与压力信息。
    def lane(request):
        calls.append(request.kind)
        assert request.pressure is pressure
        return {'cursor': {}, 'complete': True, 'items': 1, 'bytes': 0}

    for kind in tuple(storage_lanes.LANE_BINDINGS):
        _bind_lane(monkeypatch, lane, kind)
    options = {'coordinator': True, 'commit_transition': lambda operation: operation()} if coordinator else {}
    state = _run(lane_home, **options)
    assert calls == list(storage_lanes.LANE_EVICTION_ORDER)
    assert state['pressure'] is pressure and lane_home.owner_retention_json.read_bytes() == before


# 函数用途: 高水位重到期遵守300秒边界，也不越过显式关闭和零间隔。
@pytest.mark.parametrize('level', ['high', 'critical'])
def test_pressure_redue_boundary(lane_home, monkeypatch, level):
    monkeypatch.setattr(owner_quota, 'owner_quota_pressure', lambda root: {'level': level}, raising=False)
    path = lane_home.owner_data_dir / 'maintenance.json'
    path.write_text(json.dumps({'last_attempt_at': 2_000_000}))
    assert not owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_299)
    assert owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_301)
    policy = json.loads(lane_home.owner_retention_json.read_text())
    for changes in ({'maintenance_enabled': False}, {'maintenance_enabled': True, 'maintenance_interval_seconds': 0}):
        lane_home.owner_retention_json.write_text(json.dumps({**policy, **changes}))
        assert not owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_301)


# 函数用途: 拒绝重复、缺项、未知项及非法类型的调度顺序，领域动作不能先执行。
@pytest.mark.parametrize('order', [(), ('display_archives',), ('unknown',), ('long_term',) * 36, ([],) * 36])
def test_dispatcher_rejects_invalid_order(order):
    with pytest.raises(ValueError, match='^invalid_maintenance_order$'):
        owner_maintenance.run_storage_maintenance_slice({}, {}, order=order)


# 函数用途: 显式压力顺序从首项开始，不受上一片next_kind左右；默认路径仍轮转。
def test_explicit_order_ignores_previous_rotation():
    registry = tuple(kind.kind for kind in home_retention.storage_kind_registry())
    order = ('display_archives',) + tuple(kind for kind in registry if kind != 'display_archives')
    seen = []

    # 函数用途: 记录实际获得本片预算的类别。
    def lane(cursor, budget):
        seen.append(True)
        return {'cursor': {}, 'complete': True, 'items': 1, 'bytes': 0}

    state = owner_maintenance.run_storage_maintenance_slice({'next_kind': 'process_sessions'},
        {'display_archives': lane}, order=order, max_items=1)
    assert seen == [True] and state['completed'] == ['display_archives']


# 函数用途: 经过真实临时C/claim/执行ports验收新lane的原commit和最终游标持久化。
def test_actual_coordinator_entry_persists_lane_cursor_through_original_ports(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_background_curator_transactions import (
        curator_case,
        run_original,
    )

    _missing_lanes(monkeypatch)
    received = []

    # 函数用途: 通过真实请求携带的写门产生本所有者的一项临时领域效果。
    def lane(request):
        received.append(request)
        path = request.home.owner_tmp_dir / 'lane-proof.txt'
        request.run_write(lambda: path.write_text('confirmed'))
        return {'cursor': {'after': 'actual-c'}, 'complete': True, 'items': 1, 'bytes': 0}

    kind = _bind_lane(monkeypatch, lane)
    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        response = run_original(agent, gate, claim)
        assert response['ok'] is True and len(received) == 1
        assert received[0].coordinator is True and received[0].commit is not None
        marker = json.loads((agent.home_paths.owner_data_dir / 'maintenance.json').read_text())
        assert marker['storage_maintenance']['cursors'][kind] == {'after': 'actual-c'}
        assert (agent.home_paths.owner_tmp_dir / 'lane-proof.txt').read_text() == 'confirmed'


# 函数用途: 经过原维护入口跑两拍，未完或失败的领域不能被仅配额续扫吞掉。
@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('first_result', ['pending', 'error'])
def test_original_entry_retries_lane_on_next_tick(lane_home, monkeypatch, coordinator, first_result):
    received = []

    # 函数用途: 第一拍留下真实欠账，第二拍必须看到原游标并完成同一领域。
    def lane(request):
        received.append(dict(request.cursor))
        if len(received) == 1 and first_result == 'error':
            raise ValueError('domain_retry_required')
        return {'cursor': {'after': 'page-1' if len(received) == 1 else 'done'},
                'complete': len(received) > 1, 'items': 1, 'bytes': 0}

    kind = _bind_lane(monkeypatch, lane)
    options = {'include_global_indexes': False}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options).ran
    path = lane_home.owner_data_dir / 'maintenance.json'
    first = json.loads(path.read_text())
    assert first['maintenance_partial'] is True
    assert kind in first['storage_maintenance']['pending_kinds']
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_001, **options).ran
    assert received == [{}, {} if first_result == 'error' else {'after': 'page-1'}]
    second = json.loads(path.read_text())
    assert second['storage_maintenance']['cursors'][kind] == {'after': 'done'}
    assert kind not in second['storage_maintenance']['pending_kinds']
    assert not second['storage_maintenance']['errors']
    assert not second['storage_maintenance']['continuation_pending']
    assert second['maintenance_partial'] is False


# 函数用途: 高水位提前到期须在原入口真正重新分派领域，并更新节拍而非只核配额。
@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('level', ['high', 'critical'])
def test_original_entry_pressure_redue_runs_lanes_again(lane_home, monkeypatch, coordinator, level):
    received = []

    # 函数用途: 观察每次真正进入领域的时刻，不产生领域写入。
    def lane(request):
        received.append(request.now)
        return {'cursor': {}, 'complete': True, 'items': 1, 'bytes': 0}

    _bind_lane(monkeypatch, lane, 'display_archives')
    options = {'include_global_indexes': False}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    policy = lane_home.owner_retention_json.read_bytes()
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options).ran
    monkeypatch.setattr(owner_quota, 'owner_quota_pressure', lambda root: {'level': level}, raising=False)
    assert not owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_299)
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_301, **options).ran
    assert received == [2_000_000.0, 2_000_301.0]
    marker = json.loads((lane_home.owner_data_dir / 'maintenance.json').read_text())
    assert marker['last_attempt_at'] == 2_000_301
    assert not owner_maintenance.owner_maintenance_due(lane_home.owner_home_dir, now=2_000_302)
    assert lane_home.owner_retention_json.read_bytes() == policy


# 函数用途: 协调者原延期过渡不能重开已完成通道，续作结束后的新周期才重开。
@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('pressure', [False, True])
def test_lane_continuation_keeps_only_pending_then_restarts_new_cycle(lane_home, monkeypatch, coordinator, pressure):
    seen = []
    pending = 'compact_checkpoints'
    kinds = ('display_archives', pending, 'process_sessions')
    if pressure:
        monkeypatch.setattr(owner_quota, 'owner_quota_pressure', lambda root: {'level': 'high'}, raising=False)

    # 函数用途: 三个真实接线中仅一个跨片，记录完成通道是否被错误重开。
    def lane(request):
        seen.append(request.kind)
        return {'cursor': {'after': 'page-1'}, 'complete': request.kind != pending or bool(request.cursor),
                'items': 1, 'bytes': 0}

    for kind in kinds:
        _bind_lane(monkeypatch, lane, kind)
    options = {'coordinator': True, 'commit_transition': lambda operation: operation()} if coordinator else {}
    first = _run(lane_home, **options)
    assert set(seen) == set(kinds)
    assert first['pending_kinds'] == ([pending] if coordinator else [pending, 'global_indexes'])
    seen.clear()
    second = _run(lane_home, {'storage_maintenance': first}, **options)
    assert seen == [pending]
    assert not second['continuation_pending']
    assert second['pending_kinds'] == ([] if coordinator else ['global_indexes'])
    seen.clear()
    _run(lane_home, {'storage_maintenance': second}, **options)
    assert set(seen) == set(kinds) and len(seen) == len(kinds)


# 函数用途: 跨拍变更保留策略后重新取得安全结论，不能沿前拍许可继续领域写入。
@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('change', ['hold', 'invalid'])
def test_lane_continuation_rechecks_changed_retention_policy(lane_home, monkeypatch, coordinator, change):
    holds = []

    # 函数用途: 用真实入口观察安全结论，不替代策略加载或保留执行。
    def lane(request):
        holds.append(request.legal_hold)
        return {'cursor': {'after': 'pending'}, 'complete': len(holds) > 1, 'items': 1, 'bytes': 0}

    _bind_lane(monkeypatch, lane)
    options = {'include_global_indexes': False}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options).ran
    assert holds == [False]
    policy = json.loads(lane_home.owner_retention_json.read_text())
    policy.update({'legal_hold': True} if change == 'hold' else {'rejected_candidate_days': 'invalid'})
    lane_home.owner_retention_json.write_text(json.dumps(policy))
    assert owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_001, **options).ran
    marker = json.loads((lane_home.owner_data_dir / 'maintenance.json').read_text())
    if change == 'hold':
        assert holds == [False, True] and marker['legal_hold'] is True
    else:
        assert holds == [False] and marker['load_errors']


@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('style', ['keyword', 'positional', 'varargs', 'kwargs'])
def test_retention_callback_receives_config_and_supported_budget(lane_home, monkeypatch, coordinator, style):
    """两条真实维护入口都保留配置身份、预算与原提交门。"""
    module = owner_maintenance_transition if coordinator else owner_maintenance
    original = module.apply_owner_retention
    config, seen = object(), []

    def invoke(home, received, options):
        seen.append((received, options.pop('scan_budget', None), options.get('commit_transition')))
        return original(home, config=received, **options)

    if style == 'keyword':
        def callback(home, *, config, scan_budget, **options):
            return invoke(home, config, {**options, 'scan_budget': scan_budget})
    elif style == 'positional':
        def callback(home, settings, /, *, scan_budget, **options):
            return invoke(home, settings, {**options, 'scan_budget': scan_budget})
    elif style == 'varargs':
        def callback(home, *settings, now, commit_transition=None, expected_policy_fingerprint=None, scan_budget):
            return invoke(home, settings[0], dict(now=now, commit_transition=commit_transition,
                expected_policy_fingerprint=expected_policy_fingerprint, scan_budget=scan_budget))
    else:
        def callback(home, **options):
            return invoke(home, options.pop('config'), options)

    monkeypatch.setattr(module, 'apply_owner_retention', callback)
    options = {'include_global_indexes': False, 'config': config}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    result = owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options)
    assert result.ran and len(seen) == 1
    assert seen[0][:2] == (config, True)
    assert callable(seen[0][2]) is coordinator
    assert (lane_home.owner_data_dir / 'maintenance.json').is_file()


@pytest.mark.parametrize('coordinator', [False, True])
def test_retention_callback_internal_typeerror_is_not_retried(lane_home, monkeypatch, coordinator):
    module = owner_maintenance_transition if coordinator else owner_maintenance
    error, calls = TypeError('callback body failed after entry'), []

    def broken(home, *, now, config=None, **options):
        calls.append(home)
        raise error

    monkeypatch.setattr(module, 'apply_owner_retention', broken)
    options = {'include_global_indexes': False}
    if coordinator:
        options['commit_transition'] = lambda operation: operation()
    with pytest.raises(TypeError) as raised:
        owner_maintenance.run_owner_retention_if_due(lane_home, now=2_000_000, **options)
    assert raised.value is error and calls == [lane_home]
    assert not (lane_home.owner_data_dir / 'maintenance.json').exists()
