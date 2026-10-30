# LLM: 使用原retention facade及maintenance.json验证登记/预算/游标，不复制领域清理器、不把未知对象当垃圾。
# 模块用途: 覆盖31类生命周期的声明完整性与安全续作。
from __future__ import annotations

import pytest

from agent_py_agent.agent.user_space import home_retention as retention
from agent_py_agent.agent.user_space import owner_maintenance as maintenance


# LLM: 登记是结构合同而非删除许可，每类必须有事实来源、策略、引用责任及显式resolver。
# 函数用途: 防止实现漏掉Gateway、outbox、迁移等增长类别。
def test_maintenance_registry_covers_all_thirty_one_kinds():
    registry = getattr(retention, 'storage_kind_registry', None)
    assert callable(registry), '31 storage kinds must be declared in the existing retention facade'
    kinds = registry()
    assert len(kinds) == len({kind.kind for kind in kinds}) == 31
    assert {'gateway_active', 'gateway_terminal', 'outbox_delivery', 'legacy_layout'} <= {kind.kind for kind in kinds}
    for kind in kinds:
        assert kind.scope and kind.authority and kind.policy_ref and kind.protected_refs
        assert kind.actions and callable(kind.resolve_paths)
        with pytest.raises(LookupError):
            kind.resolve_paths({})


# LLM: 每个领域提交后才发布该kind的游标；总item/byte预算耗尽后未执行kind留给下一拍。
# 函数用途: 验证同一份维护状态可在小预算下连续推进。
def test_maintenance_slice_budget_keeps_committed_cursor():
    run = getattr(maintenance, 'run_storage_maintenance_slice', None)
    assert callable(run), 'existing maintenance module must own the budgeted slice'
    calls = []

    # LLM: 假领域只模拟已提交原子记录，每步恰好一条；不在测试里另建文件事实。
    # 函数用途: 观察框架给领域剩余预算并推进一个来源位置。
    def step(cursor, budget):
        calls.append(cursor.get('offset', 0))
        return {'cursor': {'offset': cursor.get('offset', 0) + 1}, 'complete': False,
                'items': 1, 'bytes': 3}

    first = run({}, {'gateway_terminal': step}, max_items=1, max_bytes=8, max_seconds=1)
    assert first['partial'] is True
    assert first['cursors']['gateway_terminal'] == {'offset': 1}
    second = run(first, {'gateway_terminal': step}, max_items=1, max_bytes=8, max_seconds=1)
    assert second['cursors']['gateway_terminal'] == {'offset': 2}
    assert calls == [0, 1]


# LLM: 领域可能先修改传入对象再失败，框架不能保存这个未提交的位置。
# 函数用途: 验证异常不推进原游标，也不将未知副作用写成complete。
def test_maintenance_slice_failure_preserves_input_cursor():
    run = getattr(maintenance, 'run_storage_maintenance_slice', None)
    assert callable(run)
    previous = {'cursors': {'gateway_terminal': {'offset': 7}}}

    # LLM: 返回前故障不构成可发布提交，旧来源位置仍承担重放责任。
    # 函数用途: 模拟领域提交回执缺失。
    def failed(cursor, budget):
        cursor['offset'] = 999
        raise OSError('commit unconfirmed')

    result = run(previous, {'gateway_terminal': failed}, max_items=1, max_bytes=8, max_seconds=1)
    assert previous['cursors']['gateway_terminal']['offset'] == 7
    assert result['cursors']['gateway_terminal']['offset'] == 7
    assert result['partial'] is True and result['errors'][0]['kind'] == 'gateway_terminal'


# LLM: partial不能被每日周期饿死，但维护关闭/零间隔的既有禁用语义仍优先。
# 函数用途: 验证持久状态的下一拍续作不等待完整一天。
def test_maintenance_partial_is_due_before_normal_interval(tmp_path):
    import json
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/maintenance.json').write_text(json.dumps({'last_attempt_at': 100, 'maintenance_partial': True}))
    assert maintenance.owner_maintenance_due(tmp_path, now=101) is True


# LLM: 第一类持续有积压不能饿死后续类别，调度位置与各领域来源游标必须独立持久化。
# 函数用途: 验证item预算为1时，下一拍轮转到下一已绑定类别。
def test_maintenance_slice_rotates_without_starving_later_kind():
    calls = []

    # LLM: 两类均未完成且每次提交一条，观察统一预算公平性而非伪造清理结果。
    # 函数用途: 生成一个可观察的领域提交适配器。
    def adapter(name):
        # LLM: 成功返回才发布递增位置，不访问任何业务目录。
        # 函数用途: 模拟一条已确认的原子提交。
        def step(cursor, budget):
            calls.append(name)
            return {'cursor': {'offset': cursor.get('offset', 0) + 1}, 'complete': False, 'items': 1, 'bytes': 1}
        return step

    adapters = {'long_term': adapter('memory'), 'gateway_terminal': adapter('gateway')}
    first = maintenance.run_storage_maintenance_slice({}, adapters, max_items=1, max_seconds=10)
    second = maintenance.run_storage_maintenance_slice(first, adapters, max_items=1, max_seconds=10)
    assert calls == ['memory', 'gateway']
    assert second['cursors']['long_term'] == {'offset': 1}
    assert second['cursors']['gateway_terminal'] == {'offset': 1}


# LLM: 原maintenance.json是唯一状态权威；结果字段变化不能丢掉未理解的扩展或续作游标。
# 函数用途: 验证旧写者更新维护结果时保留未知字段及partial责任。
def test_maintenance_result_preserves_unknown_fields_and_partial(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.common.json_io import locked_json_path

    monkeypatch.setattr(maintenance, '_outcome_fields', lambda *args: {
        'status': 'success', 'apply_outcome': 'applied', 'isolated_error_count': 0, 'failed_action_count': 0})
    path = tmp_path / 'maintenance.json'
    previous = {'schema_generation': 1, 'future': {'pins': ['keep']}}
    path.write_text(json.dumps(previous))
    retention = SimpleNamespace(load_errors=[], legal_hold=False)
    effects = {'storage_maintenance': {'partial': True, 'cursors': {'long_term': {'offset': 2}}}}
    with locked_json_path(path):
        maintenance._write_maintenance_result(path, retention, previous, 100.0, effects)
    record = json.loads(path.read_text())
    assert record['future'] == {'pins': ['keep']}
    assert record['maintenance_partial'] is True
    assert record['storage_maintenance']['cursors']['long_term'] == {'offset': 2}


# LLM: 高代际在任何结果计算和持久写之前拒绝，不能先做维护再失败于最终状态写入。
# 函数用途: 验证原状态写入口拒绝不支持的存储。
def test_maintenance_result_newer_generation_refuses_before_outcome(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts.optimization_contracts import StoreSchemaNewerError
    monkeypatch.setattr(maintenance, '_outcome_fields', lambda *args: pytest.fail('newer generation outcome must not run'))
    with pytest.raises(StoreSchemaNewerError):
        maintenance._write_maintenance_result(tmp_path / 'maintenance.json', None, {'schema_generation': 2}, 100.0, {})
    assert not (tmp_path / 'maintenance.json').exists()


# LLM: 高代际状态不能只在最终写回拒绝；普通到期与C/W路径必须先于任何业务清理识别。
# 函数用途: 验证两种维护入口都不会先清理再因版本失败。
@pytest.mark.parametrize('mode', ['due', 'transition'])
def test_maintenance_newer_state_refuses_before_any_domain_work(tmp_path, monkeypatch, mode):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.contracts.optimization_contracts import StoreSchemaNewerError
    from agent_py_agent.agent.user_space.owner_maintenance_transition import run_with_transition

    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/maintenance.json').write_text(json.dumps({'schema_generation': 2}))
    home = SimpleNamespace(owner_home_dir=tmp_path, owner_retention_json=tmp_path / 'retention.json')
    monkeypatch.setattr(maintenance, '_cleanup_owner_runtime', lambda *args, **kwargs: pytest.fail('newer state must not clean'))
    with pytest.raises(StoreSchemaNewerError):
        if mode == 'due':
            maintenance.owner_maintenance_due(tmp_path, now=100)
        else:
            run_with_transition(home, 100, lambda operation: operation())


# LLM: 回执丢失后实际工作量未知，同拍不能把未确认额度再次分给别的领域。
# 函数用途: 防止一次有界维护在异常后重复花掉同一份预算。
def test_maintenance_uncertain_adapter_stops_slice_before_next_kind():
    calls = []

    # LLM: 模拟提交后回执丢失，不能据异常推断没有效果。
    # 函数用途: 制造有未知副作用的领域失败。
    def first(cursor, budget):
        calls.append('first')
        raise OSError('ack missing after effect')

    # LLM: 后续领域有真实可调用入口，但未知额度必须阻止本拍继续执行。
    # 函数用途: 观察预算是否被重复分配。
    def second(cursor, budget):
        calls.append('second')
        return {'cursor': {}, 'complete': True, 'items': 1, 'bytes': 1}

    result = maintenance.run_storage_maintenance_slice({}, {'long_term': first, 'gateway_terminal': second}, max_items=1)
    assert calls == ['first']
    assert result['partial'] and result['errors'][0]['side_effects_unknown']
    assert 'long_term' not in result['cursors']


# LLM: NaN/无限值及布尔不是合法时间额度，须先于任何领域动作拒绝，不能退化为无限运行。
# 函数用途: 验证时间预算始终是非负有限数。
@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), True])
def test_maintenance_rejects_nonfinite_time_budget(value):
    with pytest.raises(ValueError, match='invalid_maintenance_budget'):
        maintenance.run_storage_maintenance_slice({}, {'long_term': lambda *args: pytest.fail('invalid budget must not execute')}, max_seconds=value)


# LLM: item/byte额度为准确整数，不允许布尔、小数或NaN改变边界判断。
# 函数用途: 验证预算类型不因Python比较兼容而被偷偷接受。
@pytest.mark.parametrize('name', ['max_items', 'max_bytes'])
@pytest.mark.parametrize('value', [True, 1.5, float('nan')])
def test_maintenance_rejects_noninteger_count_budget(name, value):
    with pytest.raises(ValueError, match='invalid_maintenance_budget'):
        maintenance.run_storage_maintenance_slice({}, {}, **{name: value})


# LLM: 所有本地类别须绑定，host共享投影须显式延期；保留核验不能冒充业务内容清理。
# 函数用途: 验证默认入口没有悬空类别并返回真实的保留核验范围。
def test_default_adapters_bind_every_kind_with_explicit_preservation(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    policy = SimpleNamespace(load_errors=[], applied=True, legal_hold=False)
    state = maintenance.run_owner_storage_maintenance_slice(
        home, {}, policy, now=2_000_000, quota_max_ms=1000, quota_max_entries=4096)
    for _ in range(8):
        if not state['continuation_pending']:
            break
        state = maintenance.run_owner_storage_maintenance_slice(
            home, {'storage_maintenance': state}, policy, now=2_000_000,
            quota_max_ms=1000, quota_max_entries=4096)
    assert state['unbound_kinds'] == []
    assert set(state['cursors']) == {kind.kind for kind in retention.storage_kind_registry()}
    assert state['coordinator_deferred'] == ['global_indexes']
    assert state['cursors']['long_term']['mode'] == 'preserve_only'
    assert state['cursors']['long_term']['content_verified'] is False
    assert state['cursors']['long_term']['anchor_status'] in {'present', 'missing'}
    assert state['continuation_pending'] is False


# LLM: 回执前CAS必须核完整二元组；不同类别和升级/换epoch均不能提交新游标。
# 函数用途: 验证领域执行中发生代际冲突时保留旧游标并停止本拍。
@pytest.mark.parametrize('kind', ['long_term', 'runtime_events', 'skill_registry', 'cache_tmp_trash'])
@pytest.mark.parametrize('field,value', [('schema_generation', 2), ('maintenance_epoch', 9)])
def test_adapter_fence_rejects_conflict_before_cursor_commit(kind, field, value):
    source = {'schema_generation': 1, 'maintenance_epoch': 8}
    previous = {**source, 'cursors': {kind: {'offset': 7}}}

    # LLM: 真实框架仍处理回执；只模拟另一维护代次在领域返回前接管。
    # 函数用途: 注入来源二元组变化而不修改旧游标。
    def step(cursor, budget):
        source[field] = value
        return {'cursor': {'offset': 99}, 'complete': True, 'items': 1, 'bytes': 0}

    result = maintenance.run_storage_maintenance_slice(
        previous, {kind: step}, read_fence=lambda: source, max_seconds=1)
    assert result['cursors'][kind] == {'offset': 7}
    assert result['partial'] and result['errors'][0]['kind'] == kind
    assert result['errors'][0]['error_code'] in {'STORE_SCHEMA_NEWER', 'MAINTENANCE_FENCE_CONFLICT'}


# LLM: 已过期的来源在adapter开始前拒绝，不能等到写后才发现旧epoch。
# 函数用途: 验证零领域副作用的首门CAS。
def test_adapter_fence_rejects_stale_epoch_before_execution():
    source = {'schema_generation': 1, 'maintenance_epoch': 9}
    previous = {'schema_generation': 1, 'maintenance_epoch': 8}
    with pytest.raises(ValueError, match='MAINTENANCE_FENCE_CONFLICT'):
        maintenance.run_storage_maintenance_slice(previous, {
            'long_term': lambda *args: pytest.fail('stale adapter executed')}, read_fence=lambda: source)


# LLM: 默认维护状态写者须对原canonical二元组CAS，不能拿旧参数覆盖后来epoch。
# 函数用途: 在真实临时文件上验证最终状态提交拒绝接管冲突。
def test_maintenance_result_fences_canonical_epoch_before_write(tmp_path, monkeypatch):
    import json

    path = tmp_path / 'maintenance.json'
    path.write_text(json.dumps({'schema_generation': 1, 'maintenance_epoch': 9}))
    before = path.read_bytes()
    monkeypatch.setattr(maintenance, '_outcome_fields', lambda *args: pytest.fail('stale outcome'))
    with pytest.raises(ValueError, match='MAINTENANCE_FENCE_CONFLICT'):
        maintenance._write_maintenance_result(path, None, {'schema_generation': 1, 'maintenance_epoch': 8}, 100, {})
    assert path.read_bytes() == before


# LLM: 前周期complete不能免除本周期尚未执行的核验，预算耗尽仍应有持久待续集合。
# 函数用途: 复现旧完成游标掩盖新周期零工作的错误。
def test_new_cycle_budget_exhaustion_keeps_pending_despite_old_complete():
    previous = {'cursors': {'long_term': {'complete': True}, 'gateway_terminal': {'complete': True}}}
    adapters = {key: lambda *args: pytest.fail('budget exhausted') for key in previous['cursors']}
    result = maintenance.run_storage_maintenance_slice(previous, adapters, should_yield=lambda: True)
    assert set(result['pending_kinds']) == set(adapters)
    assert result['completed'] == [] and result['partial']


# LLM: 默认入口必须消费本周期pending集合，不能用旧cursor complete掩盖尚未执行的配额和回执。
# 函数用途: 在真实默认适配器入口验证新周期零预算仍要求立即续作。
def test_default_new_cycle_zero_work_is_pending(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    policy = SimpleNamespace(load_errors=[], applied=True, legal_hold=False)
    state = maintenance.run_owner_storage_maintenance_slice(
        home, {}, policy, now=2_000_000, quota_max_ms=1000, quota_max_entries=4096)
    for _ in range(8):
        if not state['continuation_pending']:
            break
        state = maintenance.run_owner_storage_maintenance_slice(
            home, {'storage_maintenance': state}, policy, now=2_000_000,
            quota_max_ms=1000, quota_max_entries=4096)
    assert not state['continuation_pending']
    original = maintenance.run_storage_maintenance_slice

    # LLM: 只让框架预算门主动让位，真实默认适配器及结果汇总不替换。
    # 函数用途: 注入本拍在首个类别前耗尽预算的事实。
    def exhausted(*args, **kwargs):
        return original(*args, **kwargs, should_yield=lambda: True)

    monkeypatch.setattr(maintenance, 'run_storage_maintenance_slice', exhausted)
    result = maintenance.run_owner_storage_maintenance_slice(
        home, {'storage_maintenance': state}, policy, now=2_100_000)
    assert result['continuation_pending']
    assert {'cache_tmp_trash', 'gateway_terminal'} <= set(result['pending_kinds'])
    assert result['completed'] == []


# LLM: 原状态epoch坏类型必须先于保留/清理，普通和transition路径不能等到最终写状态才拒绝。
# 函数用途: 验证完整维护头在任何领域副作用之前关闭式校验。
@pytest.mark.parametrize('mode', ['due', 'transition'])
def test_invalid_epoch_refuses_before_any_domain_work(tmp_path, monkeypatch, mode):
    import json
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.owner_maintenance_transition import run_with_transition

    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/maintenance.json').write_text(json.dumps({'schema_generation': 1, 'maintenance_epoch': True}))
    home = SimpleNamespace(owner_home_dir=tmp_path, owner_retention_json=tmp_path / 'retention.json')
    monkeypatch.setattr(maintenance, '_cleanup_owner_runtime', lambda *a, **kw: pytest.fail('invalid epoch ran cleanup'))
    with pytest.raises(ValueError, match='MAINTENANCE_EPOCH_INVALID'):
        if mode == 'due':
            maintenance.owner_maintenance_due(tmp_path, now=100)
        else:
            run_with_transition(home, 100, lambda operation: operation())


# LLM: 核父目录后换成外部链接，叶stat必须仍相对已绑定FD，不能解析链接读取外部元数据。
# 函数用途: 用真实临时目录替换验证保留核验的祖先竞态防护。
def test_preservation_anchor_uses_bound_parent_fd_during_swap(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    target = home.owner_memory_long_term_jsonl
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / target.name).write_bytes(b'outside')
    original = nofollow_fs.open_directory_beneath
    original_stat = os.stat
    assert original_stat in os.supports_dir_fd
    expected_parent = target.parent.stat().st_ino
    leaf_parents = []
    swapped = []

    # LLM: 只在真实安全原语已返回父FD后注入路径替换，后续元数据应绑定旧目录。
    # 函数用途: 模拟检查与使用之间的祖先目录变化。
    def open_then_swap(root, parts, **kwargs):
        fd = original(root, parts, **kwargs)
        if tuple(parts) == target.parent.relative_to(home.owner_home_dir).parts and not swapped:
            target.parent.rename(target.parent.with_name('memory-original'))
            target.parent.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return fd

    # LLM: 记录真实叶stat使用的父inode，必须仍是原目录而非链接指向的外部目录。
    # 函数用途: 核实已绑定FD没有在路径替换后失守。
    def observe_stat(path, *args, **kwargs):
        if path == target.name and kwargs.get('dir_fd') is not None:
            leaf_parents.append(os.fstat(kwargs['dir_fd']).st_ino)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(nofollow_fs, 'open_directory_beneath', open_then_swap)
    # 透明stat包装仍调用同一真实dir_fd原语；同步函数身份探测，不能把观察器误判成平台不支持。
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {observe_stat})
    monkeypatch.setattr(os, 'stat', observe_stat)
    kind = next(kind for kind in retention.storage_kind_registry() if kind.kind == 'long_term')
    result = retention.preservation_receipt(kind, home, {}, {})
    assert swapped and result['cursor']['anchor_status'] == 'present'
    assert result['cursor']['anchor_access'] == 'dir_fd_nofollow'
    assert leaf_parents == [expected_parent]
