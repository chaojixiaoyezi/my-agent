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
