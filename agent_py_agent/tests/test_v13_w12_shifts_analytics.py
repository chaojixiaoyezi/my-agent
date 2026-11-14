"""通过真实 V13 边界验证班表、快照、效能与规则作业合同。"""
import ast
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces import v13_w12_shifts_analytics as module
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as foundation
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests import test_v13_w12_asset_discovery as asset_tests

host = asset_tests.host
failed = asset_tests.failed
idem = asset_tests.idem
invoke = asset_tests.invoke
ok = asset_tests.ok
v13_native = harness.v13_native
v13_call = harness.v13_call
v13_space = harness.v13_space


# LLM: 测试日期明确指定 UTC，不依赖运行机器本地时区。
# 函数用途: 把测试时间转成 epoch 秒。
def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# LLM: 记录时间由同一可注入时钟生成。
# 函数用途: 构造规范 UTC 测试时间。
def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: 用真实已签发上下文调用公开 helper，不伪造上下文。
# 函数用途: 构造本 owner 的 V13Call。
def call_for(host, action='shifts.schedule.get', params=None):
    context = api.bind_business_context(host, subject_id='alice', authorize=lambda action, facts: True)
    return foundation.V13Call(action, {} if params is None else params, 'helper-test', host.home_paths.owner_id,
        'alice', context, api.BusinessServices(host), host.home_paths.owner_home_dir, idem(), None, host.business_v13_clock)


# LLM: 班次 days 对应开始日，数据固定但不依赖当前真实日期。
# 函数用途: 构造精确 ShiftSchema 的测试班次。
def shift():
    return {'shift_id': 'night', 'name': '夜班', 'start': '22:00', 'end': '06:00', 'days': ['mon'],
        'members': ['alice', 'bob'], 'lead': 'alice'}


# LLM: 只通过原生写动作设置班表，不手写底层文件。
# 函数用途: 设置一个合法的首版班表。
def schedule(host):
    return ok(host, 'shifts.schedule.set', {'timezone': 'America/Los_Angeles', 'shifts': [shift()]},
        key=idem(), revision=0)['schedule']


# LLM: 种子数据沿 Store 事务；集合文件、回执和审计形状由底座生成。
# 函数用途: 写入独立测试的规范集合记录。
def seed(host, collection, rows):
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)

    # LLM: 测试修订和元数据仍显式维护，不假设 Store 自动补齐。
    # 函数用途: 把测试提供的记录合入真实集合。
    def mutate(records):
        records.update(deepcopy(rows))
        return {'count': len(rows)}

    return store.transaction(collection, mutate, idempotency_key=idem(), params_digest=idem())


# LLM: 测试案件使用契约完整形状，sample 通过结构化 tags 标注。
# 函数用途: 构造真实格式的 W8 case。
def case(host, case_id='case-one', *, status='open', assignee='alice', tags=None):
    now = host.clock[0]
    return {'case_id': case_id, 'owner_id': host.home_paths.owner_id, 'space_id': 'space-test',
        'title': '待交接案件', 'description': '', 'severity': 'high', 'status': status, 'assignee': assignee,
        'alert_ids': [], 'merged_into': None, 'tags': [] if tags is None else tags, 'custom_values': {},
        'closed_at': stamp(now - 1000) if status in {'closed', 'resolved'} else None,
        'revision': 1, 'created_at': stamp(now - 5000), 'updated_at': stamp(now)}


# LLM: 缺省班表和跨夜、起始日、时区都要从同一个真实读入口观察。
# 函数用途: 验证排班设置、读取及供 oncall 复用的公开 helper。
def test_schedule_set_get_current_shift_overnight_and_timezone(host):
    assert ok(host, 'shifts.schedule.get') == {'schedule': None,
        'current': {'shift_id': None, 'on_duty': [], 'lead': None}}
    first = schedule(host)
    assert first['revision'] == 1
    host.clock[0] = epoch('2026-11-03T07:00:00Z')
    result = ok(host, 'shifts.schedule.get')
    assert result['current'] == {'shift_id': 'night', 'on_duty': ['alice', 'bob'], 'lead': 'alice'}
    assert module.current_shift(call_for(host)) == result['current']
    host.clock[0] = epoch('2026-11-03T13:59:59Z')
    assert ok(host, 'shifts.schedule.get')['current']['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-03T14:00:00Z')
    assert ok(host, 'shifts.schedule.get')['current']['shift_id'] is None
    host.clock[0] = epoch('2026-11-04T07:00:00Z')
    assert module.current_shift(call_for(host))['on_duty'] == []
    # end == start 代表起始日开始的跨日整班，而不是永远空班。
    full = shift() | {'start': '09:00', 'end': '09:00'}
    ok(host, 'shifts.schedule.set', {'timezone': 'UTC', 'shifts': [full]}, key=idem(), revision=1)
    host.clock[0] = epoch('2026-11-03T08:59:59Z')
    assert module.current_shift(call_for(host))['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-03T09:00:00Z')
    assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: ZoneInfo 的夏令时回拨不能用固定 UTC 偏移替代。
# 函数用途: 验证 DST 回拨当天两个真实瞬间都属于同一班次。
def test_schedule_dst_and_creation_day(host):
    row = shift() | {'start': '01:30', 'end': '03:30', 'days': ['sun']}
    ok(host, 'shifts.schedule.set', {'timezone': 'America/New_York', 'shifts': [row]}, key=idem(), revision=0)
    for at in ('2026-11-01T05:45:00Z', '2026-11-01T06:45:00Z', '2026-11-01T08:29:59Z'):
        host.clock[0] = epoch(at)
        assert module.current_shift(call_for(host))['shift_id'] == 'night'
    host.clock[0] = epoch('2026-11-01T08:30:00Z')
    assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: 重复小时的边界沿 ZoneInfo 默认首次出现，半开班次不会在第二次小时重新开启。
# 函数用途: 明确结束点落入回拨小时的排班语义。
def test_schedule_dst_repeated_end_uses_first_occurrence(host):
    row = shift() | {'start': '00:30', 'end': '01:30', 'days': ['sun']}
    ok(host, 'shifts.schedule.set', {'timezone': 'America/New_York', 'shifts': [row]}, key=idem(), revision=0)
    host.clock[0] = epoch('2026-11-01T05:15:00Z')
    assert module.current_shift(call_for(host))['shift_id'] == 'night'
    for at in ('2026-11-01T05:30:00Z', '2026-11-01T06:15:00Z'):
        host.clock[0] = epoch(at)
        assert module.current_shift(call_for(host))['shift_id'] is None


# LLM: 失败写入不能改变原班表，CAS 与幂等冲突保留各自精确错误。
# 函数用途: 验证时区、重复 ID、管理员、首版 CAS、闭合参数及未知结果负例。
def test_schedule_negative_cases(host, monkeypatch):
    params = {'timezone': 'UTC', 'shifts': [shift()]}
    failed(invoke(host, 'shifts.schedule.set', params, key=idem(), revision=1), 409, 'REVISION_CONFLICT')
    failed(invoke(host, 'shifts.schedule.set', params, key=idem()), 400, 'INVALID_SCHEMA')
    failed(invoke(host, 'shifts.schedule.set', params | {'timezone': 'No/SuchZone'}, key=idem(), revision=0),
        400, 'INVALID_SCHEMA')
    failed(invoke(host, 'shifts.schedule.set', params | {'shifts': [shift(), shift()]}, key=idem(), revision=0),
        400, 'INVALID_SCHEMA')
    host.business_native_admin = lambda: False
    failed(invoke(host, 'shifts.schedule.set', params, key=idem(), revision=0), 403, 'BUSINESS_ACCESS_DENIED')
    host.business_native_admin = lambda: True
    key = idem()
    first = ok(host, 'shifts.schedule.set', params, key=key, revision=0)
    assert ok(host, 'shifts.schedule.set', params, key=key, revision=0) == first
    failed(invoke(host, 'shifts.schedule.set', params | {'timezone': 'Europe/London'}, key=key, revision=0),
        409, 'IDEMPOTENCY_CONFLICT')
    failed(invoke(host, 'shifts.schedule.set', params, key=idem(), revision=0), 409, 'REVISION_CONFLICT')
    failed(invoke(host, 'shifts.schedule.get', {'extra': True}), 400, 'INVALID_SCHEMA')

    # LLM: 模拟真实写入口抛错，检查外层必须报告未知提交而不是假成功。
    # 函数用途: 为错误语义提供故障注入。
    def unavailable(self, collection, mutate, *, idempotency_key, params_digest, expected_revision=None, receipt_view=None):
        raise OSError('synthetic transaction failure')

    monkeypatch.setattr(V13Store, 'transaction', unavailable)
    failed(invoke(host, 'shifts.schedule.set', params, key=idem(), revision=1), 503, 'OUTCOME_UNKNOWN')


# LLM: 快照必须保持创建时值，并在真实列表入口最新在前。
# 函数用途: 验证交接创建、不可变 case 快照、幂等及分页负例。
def test_handoffs_create_snapshot_and_list_newest_first(host):
    schedule(host)
    seed(host, 'soc_cases', {'open': case(host), 'investigating': case(host, 'case-two', status='investigating'),
        'closed': case(host, 'case-closed', status='closed')})
    params = {'shift_id': 'night', 'summary': '请复核现有证据', 'to_subject': 'bob', 'open_alert_ids': ['alert-one']}
    key = idem()
    first = ok(host, 'shifts.handoffs.create', params, key=key)['handoff']
    assert first['from_subject'] == 'alice' and first['to_subject'] == 'bob'
    assert {row['case_id'] for row in first['open_cases']} == {'case-one', 'case-two'}
    assert ok(host, 'shifts.handoffs.create', params, key=key)['handoff'] == first
    failed(invoke(host, 'shifts.handoffs.create', params | {'summary': 'different'}, key=key),
        409, 'IDEMPOTENCY_CONFLICT')
    seed(host, 'soc_cases', {'open': case(host) | {'title': '新标题'}})
    host.clock[0] += 1
    second = ok(host, 'shifts.handoffs.create', params | {'include_open_cases': False}, key=idem())['handoff']
    assert second['open_cases'] == []
    listing = ok(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1})
    assert listing['items'][0]['handoff_id'] == second['handoff_id'] and listing['coverage'] == 'partial'
    cursor = listing['next_cursor']
    older = ok(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1, 'cursor': cursor})
    assert older['items'] == [first] and older['next_cursor'] is None
    assert older['items'][0]['open_cases'][0]['title'] == '待交接案件'
    failed(invoke(host, 'shifts.handoffs.list', {'shift_id': 'day', 'limit': 1, 'cursor': cursor}),
        410, 'CURSOR_EXPIRED')
    failed(invoke(host, 'shifts.handoffs.list', {'shift_id': 'night', 'limit': 1, 'cursor': cursor + 'x'}),
        410, 'CURSOR_EXPIRED')
    failed(invoke(host, 'shifts.handoffs.create', {'shift_id': 'missing', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')


# LLM: 交接只访问同 owner；不把另一 owner 的 case 带入快照。
# 函数用途: 验证伪归属记录与未设置班次的 404。
def test_handoffs_and_schedule_reject_other_owner_records(host):
    failed(invoke(host, 'shifts.handoffs.create', {'shift_id': 'night', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')
    schedule(host)
    seed(host, 'soc_cases', {'foreign': case(host) | {'owner_id': 'local/other'}})
    failed(invoke(host, 'shifts.handoffs.create', {'shift_id': 'night', 'summary': ''}, key=idem()),
        404, 'RESOURCE_NOT_FOUND')
    assert ok(host, 'shifts.handoffs.list')['items'] == []
    seed(host, 'shift_schedule', {host.home_paths.owner_id: {'owner_id': 'local/other'}})
    failed(invoke(host, 'shifts.schedule.get'), 404, 'RESOURCE_NOT_FOUND')


# LLM: 所有警报字段来自精确 W8 结构，时间可以构造明确窗口与持续时间。
# 函数用途: 生成团队效能测试警报。
def alert(host, alert_id, analyst, duration, *, sample=False, verdict='true_positive'):
    now = host.clock[0]
    return {'alert_id': alert_id, 'space_id': 'space-test', 'title': '测试警报', 'description': '',
        'severity': 'high', 'status': 'resolved', 'verdict': verdict, 'ai_confidence': 0.9,
        'disposition': None, 'rule_id': None, 'source': 'sample' if sample else 'monitor',
        'external_ref': None, 'entities': [], 'techniques': [], 'assignee': analyst, 'case_id': None,
        'tags': [], 'evidence_refs': [], 'acknowledged_at': stamp(now - duration - 90),
        'resolved_at': stamp(now - 100), 'revision': 1, 'created_at': stamp(now - duration - 100),
        'updated_at': stamp(now - 100), 'owner_id': host.home_paths.owner_id}


# LLM: 反馈原因是展示内容，分支只看 override_verdict 和 created_at。
# 函数用途: 构造明确时间顺序的人工反馈。
def feedback(host, override_id, alert_id, verdict, age):
    return {'override_id': override_id, 'alert_id': alert_id, 'space_id': 'space-test', 'rule_id': None,
        'original_verdict': 'true_positive', 'override_verdict': verdict, 'reason': '人工复核',
        'analyst': 'reviewer', 'source': 'override', 'created_at': stamp(host.clock[0] - age),
        'owner_id': host.home_paths.owner_id, 'revision': 1, 'updated_at': stamp(host.clock[0] - age)}


# LLM: 反馈覆盖、样本排除、SLA 分母及均值都以可独立核算的输入验证。
# 函数用途: 验证按 assignee 与 team 聚合的效能统计。
def test_team_analytics_per_assignee_rates(host):
    seed(host, 'soc_alerts', {'a1': alert(host, 'a1', 'alice', 600),
        'a2': alert(host, 'a2', 'alice', 3600, verdict='false_positive'),
        'b1': alert(host, 'b1', 'bob', 1200, verdict='benign'),
        'sample': alert(host, 'sample', 'alice', 1, sample=True)})
    seed(host, 'soc_cases', {'closed': case(host, status='closed'),
        'sample-case': case(host, 'sample-case', status='closed', tags=['sample'])})
    seed(host, 'soc_feedback', {'f1': feedback(host, 'f1', 'a1', 'true_positive', 80),
        'f2': feedback(host, 'f2', 'a1', 'benign', 60), 'f3': feedback(host, 'f3', 'a2', 'true_positive', 50)})
    seed(host, 'monitor_sla_policies', {'space-test': {'space_id': 'space-test',
        'owner_id': host.home_paths.owner_id, 'policies': [{'severity': 'high', 'ack_minutes': 5, 'resolve_minutes': 30}],
        'revision': 1, 'created_at': stamp(host.clock[0] - 10000), 'updated_at': stamp(host.clock[0])}})
    result = ok(host, 'analytics.team.get')
    assert result['window'] == '7d' and result['coverage'] == 'complete'
    by_name = {row['analyst']: row for row in result['analysts']}
    assert set(by_name) == {'alice', 'bob'}
    assert by_name['alice'] == {'analyst': 'alice', 'alerts_triaged': 2, 'cases_closed': 1,
        'mttr_seconds': 2100, 'false_positive_rate': 0.5, 'sla_met_rate': 0.5}
    assert by_name['bob']['mttr_seconds'] == 1200 and by_name['bob']['false_positive_rate'] == 1
    assert result['team']['alerts_triaged'] == 3 and result['team']['cases_closed'] == 1
    assert result['team']['mttr_seconds'] == 1800
    assert result['team']['false_positive_rate'] == pytest.approx(2 / 3)
    assert result['team']['sla_met_rate'] == pytest.approx(2 / 3)
    empty = ok(host, 'analytics.team.get', {'window': '1m'})
    assert empty['analysts'] == []
    assert empty['team'] == {'analyst': 'team', 'alerts_triaged': 0, 'cases_closed': 0,
        'mttr_seconds': None, 'false_positive_rate': None, 'sla_met_rate': None}
    failed(invoke(host, 'analytics.team.get', {'window': '0h'}), 400, 'INVALID_SCHEMA')


# LLM: W11 case 可只通过 alert_ids 标注样本，窗口右边界不属于本窗口。
# 函数用途: 验证关联样本排除与 [since,until) 边界。
def test_team_analytics_sample_links_and_half_open_window(host):
    now = host.clock[0]
    start = now - 3600
    sample = alert(host, 'sample-alert', 'sample-analyst', 1, sample=True)
    lower = alert(host, 'lower', 'alice', 600) | {'created_at': stamp(start - 600),
        'acknowledged_at': stamp(start), 'resolved_at': stamp(start)}
    upper = alert(host, 'upper', 'alice', 600) | {'acknowledged_at': stamp(now), 'resolved_at': stamp(now)}
    seed(host, 'soc_alerts', {'sample': sample, 'lower': lower, 'upper': upper})
    seed(host, 'soc_cases', {'linked': case(host, 'linked', status='closed') | {'alert_ids': ['sample-alert']},
        'upper-case': case(host, 'upper-case', status='closed') | {'closed_at': stamp(now)}})
    seed(host, 'soc_feedback', {'sample-feedback': feedback(host, 'sample-feedback', 'sample-alert', 'benign', 10)})
    result = ok(host, 'analytics.team.get', {'window': '1h'})
    assert result['coverage'] == 'complete'
    assert result['team']['alerts_triaged'] == 1 and result['team']['cases_closed'] == 0
    assert result['team']['mttr_seconds'] == 600 and result['team']['false_positive_rate'] == 0
    assert [row['analyst'] for row in result['analysts']] == ['alice']


# LLM: 异常和重复记录先剔除，不能通过求和放大吞吐；缺 SLA 保持未知。
# 函数用途: 验证脏数据覆盖和不完整 policy 的 null 比率。
def test_team_analytics_excludes_dirty_records_and_missing_policy(host):
    valid = alert(host, 'a1', 'alice', 600)
    negative = alert(host, 'negative', 'alice', 600) | {'resolved_at': stamp(host.clock[0] - 10000)}
    missing = alert(host, 'missing', 'alice', 600)
    missing.pop('assignee')
    seed(host, 'soc_alerts', {'a1': valid, 'duplicate': valid, 'negative': negative, 'missing': missing,
        'future': alert(host, 'future', 'alice', 1) | {'created_at': stamp(host.clock[0] + 10)}})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 600
    assert result['team']['false_positive_rate'] == 0 and result['team']['sla_met_rate'] is None
    failed(invoke(host, 'analytics.team.get', {'space_id': 'missing-space'}), 404, 'RESOURCE_NOT_FOUND')
    seed(host, 'soc_alerts', {'foreign': alert(host, 'foreign', 'alice', 600) | {'owner_id': 'local/other'}})
    failed(invoke(host, 'analytics.team.get'), 404, 'RESOURCE_NOT_FOUND')


# LLM: null 是合法的未设置目标，零仍是有效时限；不以真假值混淆二者。
# 函数用途: 验证空 SLA、混合策略及零时限的真实聚合。
def test_team_nullable_and_zero_sla_targets(host):
    seed(host, 'monitor_sla_policies', {'space-test': {'space_id': 'space-test',
        'owner_id': host.home_paths.owner_id, 'policies': [
            {'severity': 'high', 'ack_minutes': None, 'resolve_minutes': None},
            {'severity': 'medium', 'ack_minutes': 0, 'resolve_minutes': 0}],
        'revision': 1, 'created_at': stamp(host.clock[0] - 1000), 'updated_at': stamp(host.clock[0])}})
    empty = ok(host, 'analytics.team.get')
    assert empty['coverage'] == 'complete' and empty['team']['sla_met_rate'] is None
    zero = alert(host, 'medium', 'bob', 0) | {'severity': 'medium',
        'acknowledged_at': stamp(host.clock[0] - 100)}
    seed(host, 'soc_alerts', {'high': alert(host, 'high', 'alice', 600), 'medium': zero})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'complete' and result['team']['sla_met_rate'] is None
    analysts = {row['analyst']: row for row in result['analysts']}
    assert analysts['alice']['sla_met_rate'] is None and analysts['bob']['sla_met_rate'] == 1


# LLM: 坏类型与坏枚举不能拖垮整个查询，也不能被默认值掩盖。
# 函数用途: 验证聚合前剔除异常告警字段并保留 partial。
@pytest.mark.parametrize(('field', 'value'), [('assignee', ['alice']), ('tags', None),
    ('severity', 'invalid'), ('status', ['resolved']), ('verdict', {'bad': True})])
def test_team_bad_alert_fields_are_partial(host, field, value):
    seed(host, 'soc_alerts', {'valid': alert(host, 'valid', 'alice', 600),
        'invalid': alert(host, 'invalid', 'bob', 1200) | {field: value}})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 600
    assert [row['analyst'] for row in result['analysts']] == ['alice']


# LLM: 确认晚于解决是异常生命周期，不得计入完整吞吐及均值。
# 函数用途: 验证跨字段时间顺序在聚合前被拒绝。
def test_team_reversed_lifecycle_is_partial(host):
    invalid = alert(host, 'invalid', 'bob', 600) | {'acknowledged_at': stamp(host.clock[0] - 50)}
    seed(host, 'soc_alerts', {'valid': alert(host, 'valid', 'alice', 1200), 'invalid': invalid})
    result = ok(host, 'analytics.team.get')
    assert result['coverage'] == 'partial'
    assert result['team']['alerts_triaged'] == 1 and result['team']['mttr_seconds'] == 1200


# LLM: 模式验证是领域规则；假模型仅截获派发，不替代真实 Store/HTTP 测试。
# 函数用途: 验证规则作业准确 kind、输入模式和目标格式差异。
def test_rule_translate_job_kind_and_mode_validation(host, monkeypatch):
    captured = []

    # LLM: 记录 W0 精确调用参数，返回显式 DomainResult 而非根据 state 猜受理。
    # 函数用途: 隔离模型执行以检查领域派发合同。
    def start(call, kind, instruction, *, space_id=None):
        captured.append((kind, instruction, space_id))
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'operation-test',
            'job_id': 'job-test', 'task_id': 'task-test'})

    monkeypatch.setattr(foundation, 'v13_start_model_job', start)
    for params in ({'mode': 'translate', 'text': 'rule'},
            {'mode': 'translate', 'text': 'rule', 'source_format': 'sigma', 'target_format': 'sigma'},
            {'mode': 'translate', 'text': 'rule', 'target_format': 'kql'}):
        with pytest.raises(module.BusinessError) as error:
            module.rule_translate(call_for(host, 'tools.rule_translate', params))
        assert error.value.code == 'INVALID_SCHEMA'
    assert captured == []
    for params in ({'mode': 'translate', 'source_format': 'nl', 'target_format': 'sigma', 'text': '检测登录失败'},
            {'mode': 'translate', 'source_format': 'spl', 'target_format': 'kql', 'text': 'search test'},
            {'mode': 'noise_advice', 'text': 'rule'}, {'mode': 'coverage_advice', 'text': 'rule'}):
        result = module.rule_translate(call_for(host, 'tools.rule_translate', params | {'space_id': 'space-test'}))
        assert isinstance(result, DomainResult) and result.http_status == 202
        assert captured[-1][0] == 'rule_translation' and captured[-1][2] == 'space-test'
        assert json.loads(captured[-1][1].split('\nrequest=', 1)[1]) == params
    assert foundation.JOB_KINDS['rule_translation']['finalize'] is module.finalize_rule_translation


# LLM: 默认空间必须由原 W1 可见结果证明唯一，零个或多个不猜。
# 函数用途: 验证规则转换的可选 space_id 选择语义。
def test_rule_translation_default_space_is_only_unique_active_space(host, monkeypatch):
    selected = []

    # LLM: 空间查询保持真实，仅隔离模型发起端口。
    # 函数用途: 捕获最终选中的真实空间 ID。
    def start(call, kind, instruction, *, space_id=None):
        selected.append(space_id)
        return DomainResult(202, {'state': 'accepted', 'operation_id': 'op-test', 'job_id': 'job-test', 'task_id': 'task-test'})

    monkeypatch.setattr(foundation, 'v13_start_model_job', start)
    params = {'mode': 'noise_advice', 'text': 'rule'}
    with pytest.raises(module.BusinessError) as error:
        module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert error.value.code == 'INVALID_SCHEMA'
    first = ok(host, 'spaces.create', {'name': '唯一空间'}, key=idem())['space_id']
    module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert selected == [first]
    ok(host, 'spaces.create', {'name': '第二空间'}, key=idem())
    with pytest.raises(module.BusinessError) as error:
        module.rule_translate(call_for(host, 'tools.rule_translate', params))
    assert error.value.code == 'INVALID_SCHEMA' and selected == [first]


# LLM: finalizer 只解析；W0 schema 必须拒绝缺字段、额外字段与超限内容。
# 函数用途: 验证模型结果不会被补字段或编造内容修复。
def test_rule_translation_finalizer_strict_json_and_schema():
    valid = {'mode': 'translate', 'output': 'title: test', 'target_format': 'sigma', 'advice': [], 'warnings': []}
    kind = foundation.JOB_KINDS['rule_translation']
    raw = json.dumps(valid)
    assert module.finalize_rule_translation(raw) == valid
    assert validate_object(kind['finalize'](raw), foundation.v13_schema(kind['result_schema'])) == valid
    for bad in ('not json', '[]', '{"mode":"translate","mode":"noise_advice"}', '{"x":NaN}'):
        with pytest.raises((HostAPIError, module.BusinessError)):
            module.finalize_rule_translation(bad)
    for value in ({'mode': 'translate'}, valid | {'extra': True}, valid | {'output': 'x' * 20001}):
        parsed = module.finalize_rule_translation(json.dumps(value))
        assert parsed == value
        with pytest.raises(HostAPIError):
            validate_object(parsed, foundation.v13_schema(kind['result_schema']))


# LLM: 只解析传入源码；保留文档、数字常量和参数边界，不把已退役的双层标签当用途说明。
# 函数用途: 检查本分片源码合同，函数说明须为 docstring 或同缩进的紧邻注释。
def _assert_slice_static_guardrails(text, filename):
    tree = ast.parse(text)
    assert ast.get_docstring(tree)
    lines = text.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            assert not any(isinstance(value, ast.Constant) and type(value.value) in (int, float)
                for value in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.args.vararg is None and node.args.kwarg is None
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            previous = lines[start - 2] if start > 1 else ''
            marker = ' ' * node.col_offset + '#'
            comment = previous[len(marker):].strip() if previous.startswith(marker) else ''
            assert ast.get_docstring(node) or comment, (filename, node.name)


# LLM: 自然注释和 docstring 沿现行规范；模块文档、数字常量与参数断言均保留。
# 函数用途: 为班表和资产发现的真实源码保留 AST 静态回归。
def test_slice_static_guardrails():
    files = [Path(module.__file__), Path(module.__file__).with_name('v13_w12_asset_discovery.py')]
    for file in files:
        _assert_slice_static_guardrails(file.read_text(encoding='utf-8'), file.name)


# LLM: 同一通用检查器覆盖自然说明、存量双层标签、docstring 和带装饰器的函数。
# 函数用途: 防止注释格式改变再次误拒已有用途说明。
@pytest.mark.parametrize('source', [
    '# 更新扫描元数据，不重复保存永久回执。\ndef update_metadata(metadata, result):\n    pass\n',
    '# LLM: 元数据仅保存扫描观察。\n# 函数用途: 更新扫描状态。\ndef update_metadata(metadata, result):\n    pass\n',
    'def update_metadata(metadata, result):\n    """更新扫描状态。"""\n    pass\n',
    '# 更新扫描状态。\n@decorate\ndef update_metadata(metadata, result):\n    pass\n',
    'def scan_run(call):\n    """运行扫描。"""\n    # 更新扫描状态。\n    def update_metadata(metadata, result):\n        pass\n',
])
def test_slice_static_guardrails_accepts_function_documentation(source):
    _assert_slice_static_guardrails('"""分片模块。"""\n' + source, 'fixture.py')


# LLM: 缺说明、空注释、错缩进、非紧邻说明及原有常量/可变参数违规仍必须失败。
# 函数用途: 证明兼容自然注释没有关闭静态防线或借用其它函数的说明。
@pytest.mark.parametrize('source', [
    'def update_metadata(metadata, result):\n    pass\n',
    '#\ndef update_metadata(metadata, result):\n    pass\n',
    '# 更新扫描状态。\n\ndef update_metadata(metadata, result):\n    pass\n',
    '    # 错缩进的说明。\ndef update_metadata(metadata, result):\n    pass\n',
    'LIMIT = 1\n',
    '# 非法可变参数。\ndef update_metadata(*args):\n    pass\n',
    '# 非法可变参数。\ndef update_metadata(**kwargs):\n    pass\n',
])
def test_slice_static_guardrails_rejects_invalid_source(source):
    with pytest.raises(AssertionError):
        _assert_slice_static_guardrails('"""分片模块。"""\n' + source, 'fixture.py')


# LLM: 只通过原模型管理入口配置隔离 owner 的离线 echo 档，不使用真实用户配置、密钥或网络。
# 函数用途: 在原任务创建前配置该测试调用者可解析的模型档。
def _configure_native_echo(native):
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    source = native[1]
    owner = resolve_owner_home(source.home_paths.root, OwnerIdentity.provider_user('feishu', 'business-alice'))
    host = SimpleNamespace(config=source.config, home_paths=home_paths_with_owner(source.home_paths, owner))
    profile_id = str(uuid4())
    execute_model_profile_operation(host, 'add', {'profile_id': profile_id, 'profile': {
        'model_backend': 'echo', 'model_name': 'fixture-echo', 'transport': 'offline', 'capability': 'agentic'}})
    execute_model_profile_operation(host, 'set_default', {'profile_id': profile_id})


# LLM: 只推进准确 owner 的原后台车道；任务和正文必须由真实 wake 执行产生，不手改状态或补写输出。
# 函数用途: 在隔离夹具内执行规则作业并核对 canonical 任务与最终消息的关联。
def _drive_native_rule_job(native, job_id):
    from contextlib import ExitStack

    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    identity = OwnerIdentity.provider_user('feishu', 'business-alice')
    owner = resolve_owner_home(native[1].home_paths.root, identity)
    job = V13Store(owner.home_dir, owner_id=owner.owner_id).get('async_jobs', job_id)
    assert job is not None and job['owner_id'] == owner.owner_id
    with ExitStack() as stack:
        agent = api.owner_complete_host(native[0], identity, stack)
        assert agent.home_paths.owner_id == owner.owner_id
        store = agent.conversation_store
        runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
        scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store})
        reports = scheduler.tick_thread(job['agent_id'])
        assert reports, '原规则任务 wake 未被后台执行器消费'
        task = store.session_tasks.require(job['task_id'])
        assert task.status in {'done', 'completed'}, task
        assert task.conversation_request_id
        messages, errors = store.messages.recent_report(task.target_thread_id, limit=100)
        assert not errors, errors
        key = 'session_task:' + task.task_id + ':dispatch'
        publication = json.loads(store.storage.wake_dedupe_path(task.target_thread_id, key).read_text(encoding='utf-8'))
        assert publication['phase'] == 'published' and publication['retain_handled'] is True
        assert publication['thread_id'] == task.target_thread_id and publication['dedupe_key'] == key
        signal = publication['signal']
        assert signal['thread_id'] == task.target_thread_id and signal['dedupe_key'] == key
        assert signal['metadata']['session_task_id'] == task.task_id == job['task_id']
        assert signal['metadata']['origin_thread_id'] == task.sender_thread_id
        assert signal['wake_signal_id'] == publication['wake_signal_id']
        final_key = 'owner_delivery:background-wake:' + signal['wake_signal_id']
        finals = [message for message in messages if message.role == 'assistant'
            and message.metadata.get('dedupe_key') == final_key
            and message.metadata.get('assistant_part_id') == 'final']
        assert len(finals) == 1
        transcript = finals[0].metadata['conversation_request_id']
        assert transcript and any(message.role == 'assistant'
            and message.metadata.get('conversation_request_id') == transcript
            and message.metadata.get('canonical_native_messages') for message in messages)
    return job


# LLM: 真实临时 Gateway、真实助手、原后台 tick 及 echo 模型完成异步主链路，不调用外部模型。
# 函数用途: 验证原生读信封、规则 202 和非法模型输出的 JOB_RESULT_INVALID。
def test_envelopes(v13_native):
    import time

    _configure_native_echo(v13_native)
    for action in ('shifts.schedule.get', 'shifts.handoffs.list', 'analytics.team.get'):
        status, body = v13_call(v13_native, action, {}, user='business-alice')
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
            'allowed_actions', 'warnings', 'effect_outcome'}
    space_id = v13_space(v13_native)
    params = {'mode': 'noise_advice', 'text': 'synthetic echo job'}
    key = idem()
    status, accepted = v13_call(v13_native, 'tools.rule_translate', params,
        user='business-alice', idempotency_key=key)
    assert status == 202, json.dumps(accepted, ensure_ascii=False)
    assert accepted['state'] == 'accepted' and accepted['error'] is None
    job = accepted['data']
    assert set(job) == {'state', 'operation_id', 'job_id', 'task_id'}
    again_status, again = v13_call(v13_native, 'tools.rule_translate', params,
        user='business-alice', idempotency_key=key)
    assert again_status == 202 and again['data'] == job
    failed(v13_call(v13_native, 'tools.rule_translate', params | {'text': 'different'},
        user='business-alice', idempotency_key=key), 409, 'IDEMPOTENCY_CONFLICT')
    stored = _drive_native_rule_job(v13_native, job['job_id'])
    assert stored['kind'] == 'rule_translation' and stored['space_id'] == space_id
    terminal = None
    for attempt in range(200):
        status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
        assert status == 200, body
        if body['data']['state'] in {'completed', 'failed'}:
            terminal = body['data']
            break
        time.sleep(0.05)
    assert terminal is not None, body
    assert terminal['state'] == 'failed' and terminal['result'] is None
    assert terminal['error']['code'] == 'JOB_RESULT_INVALID', terminal
    same_status, same = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
    assert same_status == 200 and same['data'] == terminal


# LLM: 只固定本地模型 JSON；HTTP、原后台 tick、助手任务、消息持久化及作业回读都用真实实现。
# 函数用途: 验证规则异步任务从 202 受理到合法 JSON 完成的成功链路。
def test_rule_translation_native_success(v13_native, monkeypatch):
    import time

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse

    expected = {'mode': 'translate', 'output': 'title: synthetic rule', 'target_format': 'sigma',
        'advice': [], 'warnings': []}
    prompts = []

    # LLM: 保留真实模型后端合同，不直接改任务状态、回执或 async_jobs.get。
    # 函数用途: 为隔离测试的原生模型调用返回合成 JSON。
    def generate(self, prompt, on_chunk=None, tools=None, tool_choice=None, messages=None,
                 on_thinking_delta=None, request_options=None):
        prompts.append(prompt)
        return ModelResponse(text=json.dumps(expected), backend='echo')

    monkeypatch.setattr(EchoBackend, 'generate', generate)
    _configure_native_echo(v13_native)
    space_id = v13_space(v13_native)
    status, accepted = v13_call(v13_native, 'tools.rule_translate', {'mode': 'translate',
        'source_format': 'nl', 'target_format': 'sigma', 'text': 'synthetic rule', 'space_id': space_id},
        user='business-alice', idempotency_key=idem())
    assert status == 202 and accepted['state'] == 'accepted', accepted
    job_id = accepted['data']['job_id']
    _drive_native_rule_job(v13_native, job_id)
    terminal = None
    for attempt in range(200):
        status, body = v13_call(v13_native, 'async_jobs.get', {'job_id': job_id}, user='business-alice')
        assert status == 200, body
        if body['data']['state'] in {'completed', 'failed'}:
            terminal = body['data']
            break
        time.sleep(0.05)
    assert terminal is not None and terminal['state'] == 'completed', body
    assert terminal['error'] is None and terminal['result'] == expected
    assert prompts
    status, replay = v13_call(v13_native, 'async_jobs.get', {'job_id': job_id}, user='business-alice')
    assert status == 200 and replay['data'] == terminal
