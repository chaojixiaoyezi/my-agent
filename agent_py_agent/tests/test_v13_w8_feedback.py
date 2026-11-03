"""反馈在真实 HTTP 入口验证覆写、批量重处置、统计与受控共享形状。"""
from __future__ import annotations

import uuid

from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w8_alerts import invoke, owner, seed_alert
from agent_py_agent.tests.test_v13_w8_cases import completed, rejected

native = harness.v13_native


# LLM: 使用问题中的中性 token 例参，重复请求必须重放同一反馈。
# 函数用途: 验证覆写示例、并发 stale 原判定及反馈原子性。
def test_submit_override_issue_example_and_stale_original_verdict(native):
    row = seed_alert(native, verdict='true_positive', confidence=0.9)
    params = {'alert_id': row['alert_id'], 'original_verdict': 'true_positive',
        'override_verdict': 'benign', 'reason': '内部合规扫描器触发'}
    key = uuid.uuid4().hex
    first = completed(invoke(native, 'feedback.submit_override', params, key))
    assert first['alert']['verdict'] == 'benign' and first['alert']['disposition'] == 'benign'
    assert first['override']['original_verdict'] == 'true_positive'
    assert completed(invoke(native, 'feedback.submit_override', params, key)) == first
    error = rejected(invoke(native, 'feedback.submit_override', params), 409, 'STATE_CONFLICT')
    assert {'key': 'current_verdict', 'value': 'benign'} in error['error']['details']
    rejected(invoke(native, 'feedback.submit_override', dict(params, reason='changed'), key),
             409, 'IDEMPOTENCY_CONFLICT')
    rejected(invoke(native, 'feedback.submit_override', dict(params, alert_id='not-seen')),
             404, 'RESOURCE_NOT_FOUND')
    rejected(invoke(native, 'feedback.submit_override', params, user='business-bob'),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 规则与主实体共同决定匹配，dry_run 不改变领域集合。
# 函数用途: 验证重处置筛选、状态限制、反馈、幂等及缺失错误。
def test_redisposition_matches_similar_pending_alerts_and_dry_run(native):
    source = seed_alert(native, 'source', verdict='true_positive')
    seed_alert(native, 'similar', verdict='true_positive')
    seed_alert(native, 'other-rule', verdict='true_positive', rule_id='rule-other')
    seed_alert(native, 'other-entity', verdict='true_positive', entity={'type': 'host', 'value': 'other'})
    seed_alert(native, 'closed', verdict='true_positive', status='closed')
    saved = completed(invoke(native, 'feedback.submit_override', {'alert_id': source['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工复核'}))['override']
    params = {'override_id': saved['override_id'], 'dry_run': True}
    plan = completed(invoke(native, 'feedback.apply_redisposition', params))
    assert plan['matched'] == ['similar'] and plan['applied'] == [] and plan['dry_run'] is True
    assert plan['skipped'] == [{'alert_id': 'closed', 'reason': 'not_pending'}]
    assert completed(invoke(native, 'soc.alerts.get', {'alert_id': 'similar'}))['alert']['verdict'] == 'true_positive'
    key = uuid.uuid4().hex
    result = completed(invoke(native, 'feedback.apply_redisposition', dict(params, dry_run=False), key))
    assert result['applied'] == ['similar']
    assert completed(invoke(native, 'feedback.apply_redisposition', dict(params, dry_run=False), key)) == result
    records = completed(invoke(native, 'feedback.overrides.list', {'alert_id': 'similar'}))['items']
    assert len(records) == 1 and records[0]['source'] == 'redisposition'
    rejected(invoke(native, 'feedback.apply_redisposition', {'override_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: analyst、space、alert、since 都按结构化字段匹配，游标不能跨范围重用。
# 函数用途: 验证反馈列表过滤及分页。
def test_overrides_list_filters(native):
    for name in ('first', 'second'):
        seed_alert(native, name, verdict='true_positive')
        completed(invoke(native, 'feedback.submit_override', {'alert_id': name,
            'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': 'review'}))
    page = completed(invoke(native, 'feedback.overrides.list', {'limit': 1}))
    assert page['next_cursor']
    next_page = completed(invoke(native, 'feedback.overrides.list', {'cursor': page['next_cursor']}))
    assert next_page['items'][0]['override_id'] != page['items'][0]['override_id']
    assert len(completed(invoke(native, 'feedback.overrides.list', {'alert_id': 'first'}))['items']) == 1
    assert completed(invoke(native, 'feedback.overrides.list', {'analyst': 'not-reviewer'}))['items'] == []
    rejected(invoke(native, 'feedback.overrides.list', {'alert_id': 'first', 'cursor': page['next_cursor']}),
             503, 'CURSOR_EXPIRED')
    rejected(invoke(native, 'feedback.overrides.list', {'cursor': page['next_cursor'] + 'x'}),
             503, 'CURSOR_EXPIRED')


# LLM: 未知 AI 预测分母必须为空，不能用零冒充已观察的一致率。
# 函数用途: 验证反馈统计和 AI 预测一致率。
def test_summary_agreement_null_without_ai_verdicts(native):
    assert completed(invoke(native, 'feedback.summary', {}))['agreement_rate'] is None
    for name, confidence, override in [('unknown-model', None, 'benign'),
                                        ('confirmed', 0.9, 'true_positive'), ('changed', 0.8, 'benign')]:
        seed_alert(native, name, verdict='true_positive', confidence=confidence)
        completed(invoke(native, 'feedback.submit_override', {'alert_id': name,
            'original_verdict': 'true_positive', 'override_verdict': override, 'reason': '人工研判'}))
    result = completed(invoke(native, 'feedback.summary', {}))
    assert result['overrides_total'] == 3 and result['agreement_rate'] == 0.5
    assert result['top_rules'] == [{'rule_id': 'rule-fixture', 'overrides': 3}]
    assert sum(row['count'] for row in result['by_transition']) == result['overrides_total']


# LLM: 下游 W7/W11/W12 读取同一合同，反馈记录不携其它切片的内部状态。
# 函数用途: 验证持久反馈全字段、公开 DTO 与真正来源。
def test_feedback_record_shape_matches_contract(native):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    seed_alert(native, verdict='true_positive')
    data = completed(invoke(native, 'feedback.submit_override', {'alert_id': 'alert-101',
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': '人工覆写'}))
    row = V13Store(owner(native).home_dir).get('soc_feedback', data['override']['override_id'])
    assert set(row) == {'override_id', 'alert_id', 'space_id', 'rule_id', 'original_verdict',
        'override_verdict', 'reason', 'analyst', 'source', 'created_at', 'owner_id', 'revision', 'updated_at'}
    assert row['owner_id'] == owner(native).owner_id and row['source'] == 'override'
    assert 'owner_id' not in data['override'] and 'revision' not in data['override']


# LLM: schema 错误应在领域变更前拒绝。
# 函数用途: 验证完整信封及关闭的参数结构。
def test_envelopes(native):
    rejected(invoke(native, 'feedback.submit_override', {'alert_id': 'alert-101'}), 400, 'INVALID_SCHEMA')
    rejected(invoke(native, 'feedback.summary', {'window_days': 0}), 400, 'INVALID_SCHEMA')
    rejected(invoke(native, 'feedback.overrides.list', {'owner_id': 'forged'}), 400, 'INVALID_SCHEMA')


# LLM: 没有规则的人工反馈仍计总量，但不能创建 schema 不允许的空规则排名。
# 函数用途: 回归无规则反馈汇总。
def test_summary_counts_unruled_feedback_without_null_rule_rank(native):
    row = seed_alert(native, rule_id=None)
    completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'], 'verdict': 'benign'},
        revision=row['revision']))
    result = completed(invoke(native, 'feedback.summary', {}))
    assert result['overrides_total'] == 1 and result['top_rules'] == []
    assert result['agreement_rate'] is None


# LLM: 候选共享主实体即可匹配，候选数组的排列不应影响判定。
# 函数用途: 回归重处置对候选实体位置无依赖。
def test_redisposition_primary_entity_can_be_later_in_candidate(native):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    source = seed_alert(native, 'original', verdict='true_positive')
    candidate = seed_alert(native, 'reordered', verdict='true_positive')
    candidate['entities'].insert(0, {'type': 'user', 'value': 'other'})
    harness.v13_seed(owner(native).home_dir, 'soc_alerts', {candidate['alert_id']: candidate})
    override = completed(invoke(native, 'feedback.submit_override', {'alert_id': source['alert_id'],
        'original_verdict': 'true_positive', 'override_verdict': 'benign', 'reason': 'review'}))['override']
    result = completed(invoke(native, 'feedback.apply_redisposition', {'override_id': override['override_id']}))
    assert result['applied'] == [candidate['alert_id']]
    assert V13Store(owner(native).home_dir).get('soc_alerts', candidate['alert_id'])['verdict'] == 'benign'
