"""实体评分按固定历史输入验证；Gateway 入口与纯算法边界分别覆盖。"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import v13_w8_alerts as alerts
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w8_alerts import invoke, seed_alert
from agent_py_agent.tests.test_v13_w8_cases import completed, rejected

native = harness.v13_native


# LLM: 历史输入与期望评分由明确权重组成，不依赖模型输出。
# 函数用途: 验证 issue 示例、得分排序及实体过滤。
def test_queue_example_limit_20_sorted_by_score(native):
    seed_alert(native, 'a', 'critical', entity={'type': 'user', 'value': 'alice'})
    seed_alert(native, 'b', 'high', entity={'type': 'user', 'value': 'alice'})
    seed_alert(native, 'c', 'medium', entity={'type': 'host', 'value': 'server'})
    seed_alert(native, 'd', 'low', entity={'type': 'ip', 'value': '192.0.2.1'})
    result = completed(invoke(native, 'entity_risk.queue', {'limit': 20}))
    assert [row['risk_score'] for row in result['items']] == [80, 15, 5]
    assert result['items'][0]['alert_count'] == 2
    assert result['items'][0]['baseline_deviation'] is None
    assert {row['entity_type'] for row in result['items']} == {'user', 'host', 'ip'}
    selected = completed(invoke(native, 'entity_risk.queue', {'entity_type': 'user', 'min_score': 70}))
    assert [row['entity_value'] for row in selected['items']] == ['alice']


# LLM: 阈值等号必须覆盖，统计与队列的输入完全一致。
# 函数用途: 验证高/中/低分桶及类型统计。
def test_stats_buckets_high_ge_70_medium_ge_40(native):
    for name, severity in [('h1', 'critical'), ('h2', 'medium'), ('h3', 'low')]:
        seed_alert(native, name, severity, entity={'type': 'user', 'value': 'high'})
    for name, severity in [('m1', 'high'), ('m2', 'low'), ('m3', 'low')]:
        seed_alert(native, name, severity, entity={'type': 'host', 'value': 'medium'})
    seed_alert(native, 'l1', 'info', entity={'type': 'ip', 'value': '192.0.2.2'})
    result = completed(invoke(native, 'entity_risk.stats', {}))
    assert {key: result[key] for key in ('entities_total', 'high', 'medium', 'low')} == {
        'entities_total': 3, 'high': 1, 'medium': 1, 'low': 1}
    assert result['by_type'] == {'user': 1, 'host': 1, 'ip': 1}
    assert result['generated_at'] and result['coverage'] == 'complete'


# LLM: 较早历史不会抬高三十日风险，未知实体不能空成功。
# 函数用途: 验证单实体每日历史、衰减与未知/其它owner错误。
def test_get_profile_history_and_unknown_entity_404(native):
    seed_alert(native, 'old', 'critical', age_days=60)
    seed_alert(native, 'middle', 'high', age_days=15)
    seed_alert(native, 'recent', 'medium', age_days=1)
    params = {'entity_type': 'host', 'entity_value': 'node-fixture'}
    result = completed(invoke(native, 'entity_risk.get', params))
    assert result['entity']['risk_score'] == 30
    assert result['entity']['alert_count'] == 2
    assert len(result['history']) == 3
    assert result['entity']['baseline_deviation'] == pytest.approx(2 / 3)
    assert [row['day'] for row in result['history']] == sorted(row['day'] for row in result['history'])
    rejected(invoke(native, 'entity_risk.get', dict(params, entity_value='not-seen')), 404, 'RESOURCE_NOT_FOUND')
    rejected(invoke(native, 'entity_risk.get', params, user='business-bob'), 404, 'RESOURCE_NOT_FOUND')


# LLM: cursor 与实体筛选共同绑定；篡改和跨类型复用必须显式失败。
# 函数用途: 验证实体队列分页及闭合请求。
def test_envelopes(native):
    seed_alert(native, 'one', entity={'type': 'host', 'value': 'one'})
    seed_alert(native, 'two', entity={'type': 'host', 'value': 'two'})
    first = completed(invoke(native, 'entity_risk.queue', {'limit': 1}))
    assert first['next_cursor']
    second = completed(invoke(native, 'entity_risk.queue', {'limit': 1, 'cursor': first['next_cursor']}))
    assert second['items'][0]['entity_value'] != first['items'][0]['entity_value']
    rejected(invoke(native, 'entity_risk.queue', {'entity_type': 'user', 'cursor': first['next_cursor']}),
             410, 'CURSOR_EXPIRED')
    rejected(invoke(native, 'entity_risk.stats', {'extra': True}), 400, 'INVALID_SCHEMA')


# LLM: 算法额外覆盖日期边界、重复实体、上限与未观察数据，不替代上面的真实HTTP。
# 函数用途: 验证确定性风险的时间与计数边界。
def test_score_decay_window_and_duplicate_entity(monkeypatch):
    now = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
    call = SimpleNamespace(clock=lambda: now, params={})
    values = []
    for identifier, age, severity in [('fresh', 0, 'critical'), ('week', 8, 'high'),
                                      ('month', 31, 'critical'), ('future', -1, 'critical')]:
        stamp = datetime.fromtimestamp(now - age * 86400, timezone.utc).isoformat()
        values.append({'alert_id': identifier, 'created_at': stamp, 'severity': severity,
            'entities': [{'type': 'host', 'value': 'x'}, {'type': 'host', 'value': 'x'}]})
    monkeypatch.setattr(alerts, 'w8_rows', lambda call, collection: values)
    groups = alerts._entity_alerts(call)
    assert len(groups[('host', 'x')]) == 3
    result = alerts._risk_profile(call, ('host', 'x'), groups[('host', 'x')])
    assert result['risk_score'] == 65 and result['alert_count'] == 2
    assert result['contributing_alerts'] == ['fresh', 'week']
    assert alerts._risk_profile(call, ('host', 'x'), [values[0]] * 3)['risk_score'] == 100
