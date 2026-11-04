"""W12 指标的真实 Store、统一 execute_business 及冻结用量链路回归。"""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w12_metrics as metrics
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call, v13_action_spec
from agent_py_agent.tests.test_v13_w0_harness import v13_call
from agent_py_agent.tests.test_v13_w0_harness import v13_host as v13_host
from agent_py_agent.tests.test_v13_w0_harness import v13_native as v13_native


# LLM: 时间固定在 UTC 整点，不受实际测试时间与机器时区影响。
# 函数用途: 为种入账本生成时间戳。
def _at(case, seconds):
    return datetime.fromtimestamp(case.now + seconds, timezone.utc).isoformat()


# LLM: 所有调用经过真实注册、活跃上下文、冻结 schema 与结果校验，不直接调用假路由。
# 函数用途: 执行标准业务请求。
def _request(case, action, params=None, *, write=False):
    request = {'action': action, 'request_id': 'metrics-' + uuid4().hex, 'params': params or {}}
    if write:
        request['idempotency_key'] = 'metrics-' + uuid4().hex
    return api.execute_business(request, case.context, case.services)


# LLM: 成功必须同时验证状态码、标准信封与注册结果 schema，不能只检查存在。
# 函数用途: 返回经校验的真实动作数据。
def _data(case, action, params=None):
    status, body = _request(case, action, params)
    assert status == 200, body.get('error')
    assert body['state'] == 'completed' and body['error'] is None
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
    assert v13_action_spec(action).validate_result(body['data']) == body['data']
    return body['data']


# LLM: 只写测试隔离 owner；mutate 显式维护业务记录元数据，Store 只处理集合修订。
# 函数用途: 用真实事务种入规范或故意异常的记录。
def _seed(case, collection, records):
    for start in range(0, len(records), 100):
        chunk = records[start:start + 100]

        # LLM: 规范主键来自集合契约，不按任意 _id 字段猜测。
        # 函数用途: 合并本批测试记录。
        def mutate(rows):
            for record in chunk:
                rows[record[metrics._identity(collection)]] = record
            return {'count': len(chunk)}

        case.store.transaction(collection, mutate, idempotency_key='seed-' + uuid4().hex,
            params_digest='seed-' + uuid4().hex)


# LLM: 使用共享真实宿主夹具和 W1 空间写入口，不伪造已授权空间。
# 函数用途: 创建测试 owner、固定时钟与两份真实业务空间。
@pytest.fixture
def metrics_case(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp()
    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    v13_host.business_v13_clock = lambda: now
    context, services = api.local_business_context(v13_host)
    case = SimpleNamespace(host=v13_host, context=context, services=services, now=now,
        owner=v13_host.home_paths.owner_id, home=v13_host.home_paths.owner_home_dir)
    case.store = V13Store(case.home, owner_id=case.owner, clock=lambda: now)
    spaces = []
    for name in ('指标空间', '隔离空间'):
        status, body = _request(case, 'spaces.create', {'name': name}, write=True)
        assert status == 200, body
        spaces.append(body['data']['space_id'])
    case.space, case.other_space = spaces
    return case


# LLM: 完整遵循 W8 告警记录形状；changes 只用于明确的测试变量。
# 函数用途: 构造可审计的合成告警账本输入。
def _alert(case, identifier, changes=None):
    row = {'alert_id': identifier, 'space_id': case.space, 'owner_id': case.owner,
        'title': '账本告警', 'description': '', 'severity': 'high', 'status': 'new',
        'verdict': None, 'ai_confidence': None, 'disposition': None, 'rule_id': 'rule-a',
        'source': 'monitor', 'external_ref': None, 'entities': [], 'techniques': [],
        'assignee': 'analyst-a', 'case_id': None, 'tags': [], 'evidence_refs': [],
        'acknowledged_at': None, 'resolved_at': None, 'revision': 1,
        'created_at': _at(case, -3600), 'updated_at': _at(case, -3600)}
    row.update(changes or {})
    return row


# LLM: 完整 W8 case 没有 source 字段；样本通过规范 tag 或关联告警排除。
# 函数用途: 构造真实 Store 可持久化的 case 输入。
def _case(case, identifier, changes=None):
    row = {'case_id': identifier, 'space_id': case.space, 'owner_id': case.owner,
        'title': '账本案件', 'description': '', 'severity': 'high', 'status': 'open',
        'assignee': 'analyst-a', 'alert_ids': [], 'merged_into': None, 'tags': [],
        'custom_values': {}, 'closed_at': None, 'revision': 1,
        'created_at': _at(case, -3600), 'updated_at': _at(case, -3600)}
    row.update(changes or {})
    return row


# LLM: 反馈有准确 alert_id 与创建时间，不能只用最后一条列表位置决定覆盖。
# 函数用途: 构造规范人工反馈记录。
def _feedback(case, identifier, alert_id, changes=None):
    row = {'override_id': identifier, 'alert_id': alert_id, 'space_id': case.space,
        'rule_id': 'rule-a', 'original_verdict': 'true_positive', 'override_verdict': 'benign',
        'reason': '人工复核', 'analyst': 'analyst-a', 'source': 'override', 'owner_id': case.owner,
        'revision': 1, 'created_at': _at(case, -60), 'updated_at': _at(case, -60)}
    row.update(changes or {})
    return row


# LLM: SLA 按空间和严重度存储，分钟与空目标都来自规范契约。
# 函数用途: 种入一份真实 SLA 策略。
def _sla(case, minutes=30):
    return {'space_id': case.space, 'owner_id': case.owner, 'revision': 1,
        'created_at': _at(case, -86400), 'updated_at': _at(case, -86400),
        'policies': [{'severity': level, 'ack_minutes': None, 'resolve_minutes': minutes}
            for level in ('info', 'low', 'medium', 'high', 'critical')]}


# LLM: W8 调查关联实际 task；无 task 的缺口由动作显式返回未知。
# 函数用途: 构造规范调查运行记录。
def _investigation(case, identifier, task_id, changes=None):
    row = {'run_id': identifier, 'space_id': case.space, 'owner_id': case.owner,
        'case_id': None, 'alert_id': None, 'mode': 'analysis', 'job_id': 'job-' + identifier,
        'task_id': task_id, 'agent_id': None, 'state': 'completed', 'verdict': None,
        'summary': None, 'artifact_ids': [], 'started_at': _at(case, -1800),
        'finished_at': _at(case, -60), 'revision': 1, 'created_at': _at(case, -1800),
        'updated_at': _at(case, -60)}
    row.update(changes or {})
    return row


# LLM: 创建窗口外、确认或解决窗口内的记录必须进入响应均值；样本和其它 owner 不进入计数。
# 函数用途: 验证主仪表盘示例、真实 KPI、SLA 及空窗口。
def test_dashboard_example_window_kpis_from_seeded_alerts_and_cases(metrics_case):
    case = metrics_case
    _seed(case, 'soc_alerts', [
        _alert(case, 'open', {'severity': 'critical'}),
        _alert(case, 'done', {'status': 'resolved', 'verdict': 'true_positive',
            'acknowledged_at': _at(case, -3300), 'resolved_at': _at(case, -3000)}),
        _alert(case, 'old', {'status': 'resolved', 'created_at': _at(case, -90000),
            'acknowledged_at': _at(case, -600), 'resolved_at': _at(case, -300)}),
        _alert(case, 'sample', {'source': 'sample'}),
        _alert(case, 'foreign', {'owner_id': 'local/another'}),
        _alert(case, 'other-space', {'space_id': case.other_space}),
        _alert(case, 'future', {'created_at': _at(case, 0)})])
    _seed(case, 'soc_cases', [_case(case, 'open-case'), _case(case, 'closed-case', {
        'status': 'closed', 'created_at': _at(case, -90000), 'closed_at': _at(case, -60)}),
        _case(case, 'sample-case', {'alert_ids': ['sample']})])
    _seed(case, 'soc_feedback', [_feedback(case, 'override', 'done')])
    _seed(case, 'monitor_sla_policies', [_sla(case)])
    data = _data(case, 'metrics.dashboard', {'window': '24h', 'space_id': case.space})
    assert data['window'] == '24h'
    assert data['kpis'] == {'alerts_total': 2, 'alerts_open': 1, 'alerts_critical_open': 1,
        'cases_open': 1, 'cases_closed': 1, 'mttd_seconds': 44850.0,
        'mttr_seconds': 45150.0, 'auto_triage_ratio': 0.0, 'sla_breaches': 1}
    assert len(data['trend']) == 24 and sum(point['count'] for point in data['trend']) == 2
    empty = _data(case, 'metrics.dashboard', {'window': '1m', 'space_id': case.space})
    assert empty['kpis']['alerts_total'] == 0 and empty['kpis']['mttd_seconds'] is None


# LLM: 别名共用真实注册器，固定时钟下输出必须完全相同。
# 函数用途: 验证 SOC KPI、吞吐和 telemetry 别名。
def test_soc_metrics_and_telemetry_alias_return_identical_data(metrics_case):
    case = metrics_case
    _seed(case, 'soc_alerts', [_alert(case, 'triaged', {'status': 'acknowledged',
        'acknowledged_at': _at(case, -3500), 'verdict': 'true_positive'})])
    _seed(case, 'soc_cases', [_case(case, 'closed', {'status': 'closed', 'closed_at': _at(case, -3000)})])
    data = _data(case, 'metrics.soc')
    assert _data(case, 'telemetry.soc_metrics.get') == data
    assert data['mttd_seconds'] == 100 and data['case_mttr_seconds'] == 600
    assert data['mttr_seconds'] is None
    assert data['analysts'] == [{'analyst': 'analyst-a', 'alerts_triaged': 1, 'cases_closed': 1}]
    assert data['queue'] == {'new': 0, 'acknowledged': 1, 'in_progress': 0}


# LLM: 分组前先套窗口；旧窗口大户不能挤掉当前窗口 top，空桶与并列排序稳定。
# 函数用途: 验证小时、天、分组和 other 的完整时间序列。
def test_alerts_trend_buckets_and_grouping(metrics_case):
    case = metrics_case
    alerts = [_alert(case, 'a-' + str(index), {'rule_id': 'r-' + str(index).zfill(2)}) for index in range(22)]
    alerts.extend(_alert(case, 'old-' + str(index), {'rule_id': 'old', 'created_at': _at(case, -100000)}) for index in range(30))
    alerts.append(_alert(case, 'lower-bound', {'created_at': _at(case, -86400), 'rule_id': 'r-00'}))
    alerts.append(_alert(case, 'upper-bound', {'created_at': _at(case, 0)}))
    _seed(case, 'soc_alerts', alerts)
    data = _data(case, 'metrics.alerts_trend', {'group_by': 'rule'})
    assert len(data['series']) == 20
    assert all(len(item['points']) == 24 for item in data['series'])
    assert sum(point['count'] for item in data['series'] for point in item['points']) == 23
    assert sum(point['count'] for item in data['series'] if item['key'] == 'other' for point in item['points']) == 3
    assert not any('old' in item['key'] for item in data['series'])
    assert _data(case, 'metrics.alerts_trend', {'group_by': 'rule'}) == data
    daily = _data(case, 'metrics.alerts_trend', {'window': '3d', 'bucket': 'day', 'group_by': 'severity'})
    assert daily['bucket'] == 'day' and len(daily['series'][0]['points']) == 4
    assert daily['series'][0]['points'][0]['at'] == '2026-10-30T00:00:00+00:00'
    assert sum(point['count'] for point in daily['series'][0]['points']) == 53


# LLM: 每个 breakdown 的分母一致；空 verdict 计 none，实体数组内部重复不加倍。
# 函数用途: 验证分类统计、去样本和排名。
def test_alert_stats_breakdowns_sum_to_total(metrics_case):
    case = metrics_case
    entity = {'type': 'ip', 'value': '192.0.2.10'}
    _seed(case, 'soc_alerts', [_alert(case, 'a', {'entities': [entity, entity]}),
        _alert(case, 'b', {'severity': 'critical', 'source': 'ingest', 'rule_id': 'rule-b', 'verdict': 'benign'}),
        _alert(case, 'sample', {'source': 'sample'})])
    data = _data(case, 'metrics.alert_stats')
    assert data['total'] == 2
    for field in ('by_severity', 'by_status', 'by_source', 'by_verdict'):
        assert sum(data[field].values()) == data['total']
    assert data['by_verdict'] == {'benign': 1, 'none': 1}
    assert data['top_entities'] == [{'type': 'ip', 'value': '192.0.2.10', 'count': 1}]
    assert data['top_rules'][0] == {'rule_id': 'rule-a', 'name': None, 'count': 1}


# LLM: 规则覆盖五种确定性洞察；反馈取最后有效时间，未来反馈不能重写历史。
# 函数用途: 验证洞察证据完全来自真实集合而不是模型输出。
def test_insights_are_deterministic_rules_over_real_records(metrics_case):
    case = metrics_case
    entity = {'type': 'ip', 'value': '192.0.2.10'}
    alerts = [_alert(case, 'a-' + str(index), {'entities': [entity],
        'status': 'resolved' if index < 6 else 'new', 'verdict': 'benign' if 1 <= index < 6 else 'true_positive',
        'resolved_at': _at(case, -600) if index < 6 else None}) for index in range(12)]
    alerts.extend(_alert(case, 'old-' + str(index), {'created_at': _at(case, -90000)}) for index in range(2))
    _seed(case, 'soc_alerts', alerts)
    _seed(case, 'soc_feedback', [_feedback(case, 'early', 'a-0'),
        _feedback(case, 'future', 'a-0', {'created_at': _at(case, 1), 'override_verdict': 'true_positive'})])
    _seed(case, 'monitor_sla_policies', [_sla(case)])
    _seed(case, 'threat_indicators', [{'indicator_id': 'indicator-1', 'value': entity['value'], 'type': 'ip',
        'confidence': 90, 'family_tags': [], 'actor_attribution': [], 'first_seen_at': _at(case, -86400),
        'last_seen_at': _at(case, -60), 'sources': ['operator'], 'owner_id': case.owner, 'revision': 1,
        'created_at': _at(case, -86400), 'updated_at': _at(case, -60)}])
    data = _data(case, 'insights.soc')
    assert _data(case, 'insights.soc') == data
    indexed = {row['kind']: row['evidence'] for row in data['insights']}
    assert set(indexed) == {'noisy_rule', 'rising_severity', 'top_entity', 'sla_risk', 'known_indicator'}
    assert indexed['noisy_rule']['alerts'] == 12 and indexed['noisy_rule']['benign'] == 6
    assert indexed['rising_severity'] == {'current': 12, 'previous': 2}
    assert indexed['top_entity']['count'] == 12
    assert indexed['sla_risk']['count'] == 6
    assert indexed['known_indicator']['indicator_ids'] == ['indicator-1']


# LLM: owner、space、样本过滤和明确异常剔除都在聚合之前；坏生命周期不进入均值。
# 函数用途: 验证异常记录、重复业务主键和数据隔离。
def test_dirty_records_are_excluded_before_aggregation(metrics_case):
    case = metrics_case
    good = _alert(case, 'good')
    dirty = _alert(case, 'bad-time', {'created_at': 'not-a-time'})
    negative = _alert(case, 'negative', {'acknowledged_at': _at(case, -7200)})
    duplicate = _alert(case, 'good', {'severity': 'critical'})
    _seed(case, 'soc_alerts', [good, dirty, negative,
        _alert(case, 'other', {'space_id': case.other_space}), _alert(case, 'foreign', {'owner_id': 'local/another'})])

    # LLM: Store 的规范键可拒绝常规重复；这里故意种入别名键模拟已存在的损坏输入。
    # 函数用途: 添加业务主键重复但存储键不同的记录。
    def mutate(rows):
        rows['duplicate-key'] = duplicate
        return {'count': 1}

    case.store.transaction('soc_alerts', mutate, idempotency_key='duplicate-record-key', params_digest='duplicate')
    data = _data(case, 'metrics.alert_stats', {'space_id': case.space})
    assert data['total'] == 1 and data['coverage'] == 'partial'
    assert sum(data['by_severity'].values()) == 1
    assert _data(case, 'metrics.soc', {'space_id': case.space})['mttd_seconds'] is None


@pytest.mark.parametrize(('action', 'params'), [
    ('metrics.dashboard', {'window': '0h'}), ('metrics.dashboard', {'window': '24h', 'owner_id': 'other'}),
    ('metrics.alerts_trend', {'bucket': 'minute'}), ('metrics.alerts_trend', {'group_by': 'assignee'}),
    ('costs.investigations_aggregate', {'limit': 101}), ('insights.soc', {'window': '24H'})])
# LLM: 运输闭合 schema 必须在领域调用之前拒绝额外参数和非法值。
# 函数用途: 验证非法窗口、分组与查询字段的准确失败信封。
def test_invalid_params_fail_before_domain(metrics_case, action, params):
    status, body = _request(metrics_case, action, params)
    assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    assert body['state'] == 'failed' and body['data'] == {}


# LLM: 容量超限不能静默截掉桶或把小时改成天。
# 函数用途: 验证超过契约点数的显式失败。
def test_bucket_capacity_is_explicit(metrics_case):
    status, body = _request(metrics_case, 'metrics.alerts_trend', {'window': '32d', 'bucket': 'hour'})
    assert status == 400 and body['error']['code'] == 'LIMIT_EXCEEDED'
    assert body['data'] == {}


# LLM: 没有 SLA 时均值和违约明确空值；空窗口不可拿旧记录填成 KPI。
# 函数用途: 验证空账本的诚实值及只读不写盘。
def test_empty_metrics_and_readonly_receipts(metrics_case):
    case = metrics_case
    before = case.store.revision('audit_log')
    data = _data(case, 'metrics.dashboard')
    assert data['kpis'] == {'alerts_total': 0, 'alerts_open': 0, 'alerts_critical_open': 0,
        'cases_open': 0, 'cases_closed': 0, 'mttd_seconds': None, 'mttr_seconds': None,
        'auto_triage_ratio': None, 'sla_breaches': None}
    assert _data(case, 'metrics.alerts_trend')['series'][0]['key'] == 'all'
    assert _data(case, 'insights.soc')['insights'] == []
    assert case.store.revision('audit_log') == before


# LLM: 本地身份仍来自原发行器，撤权或不存在的空间都不能成功披露。
# 函数用途: 验证活跃授权与空间边界的真实入口。
def test_missing_space_and_live_denial(metrics_case):
    case = metrics_case
    status, body = _request(case, 'metrics.dashboard', {'space_id': 'space-absent'})
    assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND'
    denied = api.bind_business_context(case.host, authorize=lambda _action, _facts: False)
    status, body = api.execute_business({'action': 'metrics.soc', 'request_id': 'denied', 'params': {}}, denied, case.services)
    assert status == 403 and body['error']['code'] == 'CAPABILITY_DENIED'
    assert body['data'] == {}


# LLM: 损坏账本不能作为空窗口成功；关功能和缺用量 reader 也必须拒绝。
# 函数用途: 验证源失败与 readiness 的真实错误传播。
def test_unavailable_sources_and_disabled_feature(metrics_case):
    case = metrics_case
    status, body = _request(case, 'costs.dashboard')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    case.host.config.business_spaces_enabled = False
    status, body = _request(case, 'metrics.dashboard')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    case.host.config.business_spaces_enabled = True
    _seed(case, 'soc_alerts', [_alert(case, 'one')])
    path = case.home / 'data' / 'business_v13' / 'soc_alerts.json'
    path.write_text('{', encoding='utf-8')
    status, body = _request(case, 'metrics.alert_stats')
    assert status == 503 and body['data'] == {}


# LLM: 用真实 ConversationStore 追加模型事件，不手写 usage.get 返回或替换生产 reader。
# 函数用途: 在规范模型账本记录一次合成供应商用量和尝试次数。
def _append_usage(case, thread_id, task_id, event_id, input_tokens, age=-60):
    return case.host.conversation_store.model_usage.append_once({
        'event_id': event_id, 'thread_id': thread_id, 'request_id': 'request-' + event_id,
        'run_id': 'run-' + event_id, 'task_id': task_id, 'source': 'model', 'now': case.now + age,
        'model_calls': {'schema': 'model_call_summary.v1', 'physical_model_attempt_count': 2, 'models': ['model-a'],
            'model_attempt_counts': {'model-a': 2},
            'usage_breakdown': {'provider': {'input_tokens': input_tokens, 'output_tokens': 7,
                'cache_read_input_tokens': 0, 'cache_write_input_tokens': 0, 'call_count': 1}},
            'purpose_breakdown': {'schema': 'model_call_purpose_breakdown.v1', 'decision': {'physical_model_attempt_count': 2,
                'status_counts': {'finished': 2}, 'usage_breakdown': {'provider': {
                    'input_tokens': input_tokens, 'input_tokens_reported_call_count': 1}}}}}})


# LLM: 默认 reader 必须配套生产观察服务以核验聚合范围证明；不伪造事件时间、reader 返回或冻结 usage.get。
# 函数用途: 建立可查询真实用量的 owner 原生宿主。
@pytest.fixture
def usage_case(metrics_case, monkeypatch):
    from agent_py_agent.agent.conversation.background_team_lifecycle import BACKGROUND_IDENTITY_KEY
    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
        DefaultBusinessObservationService,
        default_observation_readers,
    )
    from agent_py_agent.agent.observability import concurrency_metrics
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    case = metrics_case
    store = ConversationStore(case.home / 'conversations')
    case.host.conversation_store = store
    case.task_ids = {}
    for identifier, space in (('controller', case.space), ('agent-a', case.space),
            ('agent-b', case.space), ('agent-other', case.other_space)):
        metadata = {'business_controller': True} if identifier == 'controller' else {
            BACKGROUND_IDENTITY_KEY: {'agent_id': identifier, 'managed_by_thread_id': 'controller',
                'space_id': space, 'project_ref': None, 'identity_state': 'available',
                'after_task': 'reuse', 'identity_revision': 1}}
        create_thread_atomic(store.threads, ConversationThread(thread_id=identifier, owner_id=case.owner,
            canonical_user_id=case.owner, title=identifier, metadata=metadata, model_profile_id='echo-profile'))
        if identifier != 'controller':

            # LLM: 会话任务的业务上下文绑定 Store 分配的准确 task_id。
            # 函数用途: 生成不借用其它调查身份的规范任务事实。
            def context_factory(task_id):
                return BusinessExecutionContext(case.owner, space, None, task_id, 1, None, (), (), 'business-context.v1')

            task = store.session_tasks.create(SessionTaskDraft('controller', identifier, '用量核对', dedupe_key=identifier),
                business_context_intent=BusinessContextIntent(case.owner, space, None, None),
                business_context_factory=context_factory)
            case.task_ids[identifier] = task.task_id
    _append_usage(case, 'agent-a', case.task_ids['agent-a'], 'event-a', 123)
    _append_usage(case, 'agent-b', case.task_ids['agent-b'], 'event-b', 77)
    _append_usage(case, 'agent-other', case.task_ids['agent-other'], 'event-other', 900)
    _append_usage(case, 'agent-a', case.task_ids['agent-a'], 'old-event', 5000, -100000)
    _append_usage(case, 'agent-a', case.task_ids['agent-a'], 'future-event', 7000, 0)
    runtime = RuntimeRepository(case.home / 'usage-runtime.db')
    previous_metrics = concurrency_metrics._METRICS
    monkeypatch.setattr(concurrency_metrics, '_METRICS', None)
    meter = concurrency_metrics._metrics()
    monkeypatch.setattr(meter.llm_inflight, '_vals', {})
    meter.llm_inflight.set(0, labels={'owner_id': case.owner})
    case.host.business_projection = DefaultBusinessObservationService(case.home, owner_id=case.owner,
        store=store, runtime=runtime, readers=default_observation_readers(case.home, case.owner, store, runtime))
    case.context, case.services = api.local_business_context(case.host)
    # 后台目录沿原 W2 核验真实 controller 发起人，不能用本地空 actor 冒充通道身份。
    case.context = api.bind_business_context(case.host, authorize=case.context.authorize,
        actor=HostChannelActor(case.owner, 'controller', 'metrics-request', 'feishu', 'controller', case.owner))
    yield case
    monkeypatch.setattr(concurrency_metrics, '_METRICS', previous_metrics)


# LLM: 实际 token 来自冻结 usage.get，未提供费用或模型 token 拆分就返回空值和 unknown。
# 函数用途: 验证成本仪表盘真实用量、Agent 排名、空间过滤与尝试次数。
def test_costs_dashboard_tokens_from_usage_get_and_cost_null_when_unknown(usage_case):
    case = usage_case
    filters = {'since': _at(case, -86400), 'until': _at(case, 0), 'space_id': case.space}
    status, frozen = _request(case, 'usage.get', {'filter': filters})
    assert status == 200, frozen.get('error')
    assert frozen['data']['actual']['input_tokens'] == 200
    data = _data(case, 'costs.dashboard', {'space_id': case.space})
    assert data['totals']['actual'] == frozen['data']['actual']
    assert data['totals']['estimated'] == frozen['data']['estimated']
    assert data['totals']['actual']['cost'] is None and 'cost' in data['unknown']
    assert 'tokens_by_model' in data['unknown']
    assert [row['agent_id'] for row in data['by_agent']] == ['agent-a', 'agent-b']
    assert [row['actual']['input_tokens'] for row in data['by_agent']] == [123, 77]
    assert [row['space_id'] for row in data['by_space']] == [case.space]
    assert data['by_model'] == [{'model': 'model-a', 'attempt_count': 4}]
    assert set(data['by_model'][0]) == {'model', 'attempt_count'}


# LLM: 调查按 started_at 入窗，真实 task_id 过滤不能把同线程其它任务或窗口外事件混入。
# 函数用途: 验证调查成本总计、平均值、limit 与并列确定性。
def test_investigation_costs_aggregate_per_run_task(usage_case):
    case = usage_case
    _seed(case, 'soc_investigations', [
        _investigation(case, 'run-a', case.task_ids['agent-a']),
        _investigation(case, 'run-b', case.task_ids['agent-b']),
        _investigation(case, 'old-run', case.task_ids['agent-a'], {'started_at': _at(case, -100000)}),
        _investigation(case, 'other-run', case.task_ids['agent-other'], {'space_id': case.other_space}),
        _investigation(case, 'foreign-run', case.task_ids['agent-a'], {'owner_id': 'local/another'})])
    data = _data(case, 'costs.investigations_aggregate', {'space_id': case.space, 'limit': 1})
    assert data['investigations'] == 2 and data['totals']['input_tokens'] == 200
    assert data['avg_input_tokens'] == 100
    assert len(data['top']) == 1 and data['top'][0]['run_id'] == 'run-a'
    assert data['top'][0]['actual']['input_tokens'] == 123
    assert data['totals']['cost'] is None and 'cost' in data['unknown']


# LLM: 无 task 和重复 task 都保留为可见缺口；不按重复引用把 token 翻倍。
# 函数用途: 验证不完整调查的空值传播和异常口径。
def test_investigation_missing_and_duplicate_tasks_are_not_zero(usage_case):
    case = usage_case
    _seed(case, 'soc_investigations', [_investigation(case, 'a', case.task_ids['agent-a']),
        _investigation(case, 'b', case.task_ids['agent-a']), _investigation(case, 'missing', None)])
    data = _data(case, 'costs.investigations_aggregate', {'space_id': case.space})
    assert data['investigations'] == 3 and len(data['top']) == 1
    assert {'runs_without_task', 'duplicate_tasks'} <= set(data['unknown'])
    assert data['totals']['input_tokens'] is None and data['avg_input_tokens'] is None
    assert data['coverage'] == 'partial'


# LLM: 每个指标和成本动作都须经标准信封；不以调用 helper 代替运输验收。
# 函数用途: 覆盖七个规范动作和 SOC 别名的成功结果契约。
def test_envelopes(usage_case):
    case = usage_case
    for action in ('metrics.dashboard', 'metrics.alerts_trend', 'metrics.soc', 'telemetry.soc_metrics.get',
            'metrics.alert_stats', 'insights.soc', 'costs.dashboard', 'costs.investigations_aggregate'):
        _data(case, action, {'space_id': case.space})


# LLM: 聚合辅助函数不需要价格信息，不同货币和任一空 token 都不能伪造总和。
# 函数用途: 验证用量空值与货币不相容传播。
def test_usage_measure_aggregation_keeps_unknowns():
    known = {'input_tokens': 10, 'cached_input_tokens': 2, 'uncached_input_tokens': 8,
        'output_tokens': 3, 'cost': 0.5, 'currency': 'USD', 'coverage': 'complete'}
    other = dict(known, currency='EUR', input_tokens=None)
    result = metrics._sum_measures([known, other])
    assert result['input_tokens'] is None and result['output_tokens'] == 6
    assert result['cost'] is None and result['currency'] is None


# LLM: 模型维度只读事件尝试次数，空间与窗口必须与 token 查询一致。
# 函数用途: 独立验证真实模型账本的次数投影，不依赖费用 reader 可用性。
def test_model_attempts_from_real_events_are_window_and_space_scoped(usage_case):
    case = usage_case
    call = V13Call('costs.dashboard', {'space_id': case.space}, 'attempts', case.owner,
        case.context.subject_id, case.context, case.services, case.home, clock=lambda: case.now)
    models, complete = metrics._model_attempts(call, case.now - 86400, case.now)
    assert models == [{'model': 'model-a', 'attempt_count': 4}] and complete


# LLM: W8 会把当前 verdict 更新为人工值；一致率必须保留最初 AI 裁定而不是与自己比较。
# 函数用途: 验证人工覆盖后的真实自动化一致率。
def test_agreement_uses_original_verdict_before_human_override(metrics_case):
    case = metrics_case
    _seed(case, 'soc_alerts', [_alert(case, 'overridden', {'verdict': 'benign'})])
    _seed(case, 'soc_feedback', [_feedback(case, 'feedback', 'overridden')])
    result = _data(case, 'metrics.soc')
    assert result['automation'] == {'ai_verdicts': 1, 'human_overrides': 1, 'agreement_rate': 0.0}


# LLM: 共享 fixture 启动隔离真实 HTTP Server，客户端解析真实响应，不手写 JSON 返回。
# 函数用途: 验证原生运输、SOC 别名与闭合参数失败信封。
def test_native_http_soc_alias_and_closed_params(v13_native):
    status, result = v13_call(v13_native, 'metrics.soc', {}, user='business-alice')
    alias_status, alias = v13_call(v13_native, 'telemetry.soc_metrics.get', {}, user='business-alice')
    assert status == alias_status == 200, (result, alias)
    assert result['state'] == alias['state'] == 'completed'
    assert result['error'] is None and alias['error'] is None
    assert result['data'] == alias['data']
    assert result['data']['mttd_seconds'] is None and result['data']['analysts'] == []
    status, rejected = v13_call(v13_native, 'metrics.dashboard', {'owner_id': 'other'}, user='business-alice')
    assert status == 400 and rejected['error']['code'] == 'INVALID_SCHEMA'
    assert rejected['state'] == 'failed' and rejected['data'] == {}


# LLM: usage 的记录级拒绝必须原样失败，不能因异常继承 ValueError 被吞成 unknown。
# 函数用途: 验证模型次数聚合不吞线程授权错误。
def test_model_attempts_propagate_live_thread_denial(usage_case):
    import pytest

    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    case = usage_case
    context = api.bind_business_context(case.host, subject_id=case.context.subject_id,
        authorize=lambda _action, facts: facts.get('agent_id') != 'agent-b')
    call = V13Call('costs.dashboard', {'space_id': case.space}, 'attempt-denied', case.owner,
        context.subject_id, context, case.services, case.home, clock=lambda: case.now)
    with pytest.raises(HostAPIError) as raised:
        metrics._model_attempts(call, case.now - 86400, case.now)
    assert raised.value.code == 'CAPABILITY_DENIED'


# LLM: 原领域拒绝也是 ValueError 子类，不能被来源容错分支转成未知成功。
# 函数用途: 补充验证模型次数聚合原样传播 BusinessError。
def test_model_attempts_propagate_business_access_denial(usage_case):
    from agent_py_agent.agent.conversation.business_access import BusinessError

    case = usage_case
    denied = BusinessError('BUSINESS_ACCESS_DENIED')

    # LLM: 只拒绝第二个真实后台身份，前一个身份仍按原数据读取。
    # 函数用途: 从活跃授权回调抛出真实领域拒绝。
    def authorize(action, facts):
        if action == 'usage.get' and facts.get('agent_id') == 'agent-b':
            raise denied
        return True

    context = api.bind_business_context(case.host, subject_id=case.context.subject_id, authorize=authorize)
    call = V13Call('costs.dashboard', {'space_id': case.space}, 'business-denied', case.owner,
        context.subject_id, context, case.services, case.home, clock=lambda: case.now)
    with pytest.raises(BusinessError) as raised:
        metrics._model_attempts(call, case.now - 86400, case.now)
    assert raised.value is denied
    assert raised.value.code == 'BUSINESS_ACCESS_DENIED'
