"""W11自治建议与业务上下文：原生存储、实际HTTP及负向边界回归。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.test_mega_business_api_actions import native_request

pytest_plugins = ('agent_py_agent.tests.test_business_v12_f9_e2e',)


# LLM: 测试根均来自tmp_path，不接触运行中的用户空间。
# 函数用途: 建立原生签发context及真实业务服务。
def _host(tmp_path, *, admin=True):
    paths = home_paths_with_owner(home_paths(tmp_path / 'root'), resolve_owner_home(tmp_path / 'root'))
    host = SimpleNamespace(home_paths=paths,
                           config=SimpleNamespace(business_spaces_enabled=True),
                           business_native_admin=lambda: admin,
                           business_v13_clock=lambda: datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp())
    return host


# LLM: 所有断言经过真实统一dispatcher和schema，不直接调用产品handler。
# 函数用途: 发送一笔本机业务调用并返回标准信封。
def _call(host, action, params=None, *, key=None, revision=None, authorize=None):
    context = api.bind_business_context(host, authorize=authorize or (lambda action, facts: True), subject_id='reviewer')
    request = {'action': action, 'request_id': 'test-' + action, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    return api.execute_business(request, context, api.BusinessServices(host))


# LLM: 断言同一标准信封及动作结果schema，不能把任意HTTP200当成功。
# 函数用途: 取出已校验的完成结果。
def _completed(response, action):
    status, body = response
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return v13_action_spec(action).validate_result(body['data'])


# LLM: 测试种子沿原Store事务，无手写第二持久格式。
# 函数用途: 保存符合记录边界的合成事实。
def _seed(host, collection, records):
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)

    # LLM: 合成数据只在隔离测试根创建，不碰真实运行账。
    # 函数用途: 在原Store中提交给定记录。
    def mutate(current):
        current.update(records)
        return {'seeded': len(records)}

    store.transaction(collection, mutate, idempotency_key='seed-' + collection,
                      params_digest=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest())
    return store


# LLM: 完整W8记录字段不随当前handler读取范围缩水。
# 函数用途: 创建规范样本告警。
def _alert(host, alert_id, *, verdict='true_positive', entity='core-01', created_at=None):
    at = created_at or '2026-11-01T12:00:00+00:00'
    return {'alert_id': alert_id, 'space_id': 'space-one', 'title': '合成告警', 'description': '',
            'severity': 'high', 'status': 'new', 'verdict': verdict, 'ai_confidence': 0.9,
            'disposition': 'triage', 'rule_id': None, 'source': 'ingest', 'external_ref': None,
            'entities': [{'type': 'host', 'value': entity}], 'techniques': [], 'assignee': None,
            'case_id': None, 'tags': ['core'], 'evidence_refs': [], 'acknowledged_at': None,
            'resolved_at': None, 'revision': 0, 'created_at': at, 'updated_at': at,
            'owner_id': host.home_paths.owner_id}


# LLM: 一条反馈必须保留原建议及分析师最终结构化裁决。
# 函数用途: 创建规范人机反馈记录。
def _feedback(host, identifier, alert_id, verdict, at):
    return {'override_id': identifier, 'alert_id': alert_id, 'space_id': 'space-one', 'rule_id': None,
            'original_verdict': 'true_positive', 'override_verdict': verdict, 'reason': '合成审核',
            'analyst': 'reviewer', 'source': 'override', 'created_at': at,
            'owner_id': host.home_paths.owner_id, 'revision': 0, 'updated_at': at}


# LLM: 同键重放和不同参数复用键分别验证，旧CAS不能覆盖并发新版本。
# 函数用途: 验证自治建议列表、更新、重置和版本单调性。
def test_policy_list_update_reset_cas(tmp_path):
    host = _host(tmp_path)
    initial = _completed(_call(host, 'autonomy_policy.list'), 'autonomy_policy.list')
    assert initial == {'policies': [], 'default_level': 'manual_approval', 'enforcement': 'advisory', 'revision': 0}
    params = {'action': 'isolate_host', 'level': 'shadow'}
    first = _completed(_call(host, 'autonomy_policy.update', params, key='policy-update-one', revision=0),
                       'autonomy_policy.update')
    assert first['policy']['level'] == 'shadow' and first['revision'] == 1
    assert _call(host, 'autonomy_policy.update', params, key='policy-update-one', revision=0)[1]['data'] == first
    conflict = _call(host, 'autonomy_policy.update', {**params, 'level': 'autonomous'}, key='policy-update-one', revision=0)
    assert conflict[0] == 409 and conflict[1]['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    stale = _call(host, 'autonomy_policy.reset', {'action': 'isolate_host'}, key='policy-reset-old', revision=0)
    assert stale[0] == 409 and stale[1]['error']['code'] == 'REVISION_CONFLICT'
    reset = _completed(_call(host, 'autonomy_policy.reset', {'action': 'isolate_host'},
                             key='policy-reset-new', revision=1), 'autonomy_policy.reset')
    assert reset == {'action': 'isolate_host', 'level': 'manual_approval', 'revision': 2}
    assert _call(host, 'autonomy_policy.list')[1]['data']['revision'] == 2


# LLM: 影子模式只改建议，CAS与其它策略共用规范最高版本。
# 函数用途: 验证影子分类读取、设置和旧版本拒绝。
def test_shadow_mode_get_set(tmp_path):
    host = _host(tmp_path)
    assert _call(host, 'autonomy_policy.shadow_mode.get')[1]['data'] == {'classes': [], 'revision': 0}
    value = _completed(_call(host, 'autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True},
                             key='shadow-set-class', revision=0), 'autonomy_policy.shadow_mode.set')
    assert value['class']['shadow'] is True and value['revision'] == 1
    assert _call(host, 'autonomy_policy.shadow_mode.get')[1]['data']['classes'] == [value['class']]
    assert _call(host, 'autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': False},
                 key='shadow-stale-key', revision=0)[0] == 409


# LLM: 授权只保存在当前owner，过期与撤销不混同，撤销不恢复。
# 函数用途: 验证建议授权创建、幂等、过期过滤和最终撤销。
def test_grants_create_list_revoke(tmp_path):
    host = _host(tmp_path)
    params = {'action': 'isolate_host', 'level': 'shadow', 'expires_in_hours': 1}
    first = _completed(_call(host, 'autonomy_policy.grants.create', params, key='grant-create-one'),
                       'autonomy_policy.grants.create')['grant']
    assert _call(host, 'autonomy_policy.grants.create', params, key='grant-create-one')[1]['data']['grant'] == first
    assert _call(host, 'autonomy_policy.grants.list')[1]['data']['items'] == [first]
    now = host.business_v13_clock()
    host.business_v13_clock = lambda: now + 3601
    assert _call(host, 'autonomy_policy.grants.list')[1]['data']['items'] == []
    assert _call(host, 'autonomy_policy.grants.list', {'include_expired': True})[1]['data']['items'] == [first]
    revoked = _completed(_call(host, 'autonomy_policy.grants.revoke', {'grant_id': first['grant_id']},
                               key='grant-revoke-one'), 'autonomy_policy.grants.revoke')
    assert revoked == {'grant_id': first['grant_id'], 'revoked': True}
    assert _call(host, 'autonomy_policy.grants.list', {'include_expired': True})[1]['data']['items'] == []
    absent = _call(host, 'autonomy_policy.grants.revoke', {'grant_id': 'missing'}, key='grant-missing-key')
    assert absent[0] == 404 and absent[1]['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 同一个告警多次修订只计最后反馈，窗口外、未来及其它owner不得混入。
# 函数用途: 从真实存储反馈计算7d人机一致率。
def test_agreement_example_window_7d_from_feedback(tmp_path):
    host = _host(tmp_path)
    _seed(host, 'soc_alerts', {'a': _alert(host, 'a'), 'b': _alert(host, 'b'),
          'c': _alert(host, 'c', verdict=None), 'old': _alert(host, 'old', created_at='2025-01-01T00:00:00+00:00')})
    _seed(host, 'soc_feedback', {
        'f1': _feedback(host, 'f1', 'a', 'false_positive', '2026-11-01T13:00:00+00:00'),
        'f2': _feedback(host, 'f2', 'a', 'true_positive', '2026-11-01T14:00:00+00:00'),
        'f3': _feedback(host, 'f3', 'b', 'false_positive', '2026-11-01T14:00:00+00:00')})
    result = _completed(_call(host, 'autonomy_policy.agreement', {'window': '7d'}), 'autonomy_policy.agreement')
    assert (result['ai_suggestions'], result['human_reviewed'], result['agreed'], result['agreement_rate']) == (2, 2, 1, 0.5)
    assert result['by_action'] == [{'action': 'triage', 'suggested': 2, 'reviewed': 2, 'agreed': 1, 'agreement_rate': 0.5}]
    empty = _call(_host(tmp_path / 'empty'), 'autonomy_policy.agreement', {'window': '7d'})[1]['data']
    assert empty['human_reviewed'] == 0 and empty['agreement_rate'] is None


# LLM: 预览只读，版本、回执和审计不应增加；匹配结果直接由合成事实计算。
# 函数用途: 验证规则创建、过滤、实体glob及影响预览。
def test_business_context_rules_create_list_preview(tmp_path):
    host = _host(tmp_path)
    store = _seed(host, 'soc_alerts', {'a': _alert(host, 'a'), 'b': _alert(host, 'b', entity='other')})
    rule = {'name': '核心服务', 'match': {'entity_type': 'host', 'pattern': 'core-*', 'tags': ['core']},
            'effect': {'severity_boost': 2, 'priority_label': '核心'}, 'enabled': True}
    created = _completed(_call(host, 'business_context.rules.create', rule, key='context-rule-key'),
                         'business_context.rules.create')['rule']
    assert _call(host, 'business_context.rules.list', {'enabled': True})[1]['data']['items'] == [created]
    before = store.revision('soc_alerts'), store.revision('business_context_rules'), store.revision('audit_log')
    result = _completed(_call(host, 'business_context.rules.preview', {'rule_id': created['rule_id']}),
                        'business_context.rules.preview')
    assert result['matched_alerts'] == 1 and result['affected_entities'] == 1
    assert result['severity_changes'] == {'high->critical': 1}
    assert result['sample'] == [{'alert_id': 'a', 'entity': 'core-01', 'before_severity': 'high', 'after_severity': 'critical'}]
    assert before == (store.revision('soc_alerts'), store.revision('business_context_rules'), store.revision('audit_log'))
    assert _call(host, 'business_context.rules.preview')[0] == 400
    assert _call(host, 'business_context.rules.preview', {'rule_id': created['rule_id'],
                 'rule': {'match': rule['match'], 'effect': rule['effect']}})[0] == 400
    assert _call(host, 'business_context.rules.preview', {'rule_id': 'missing'})[0] == 404
    other = _host(tmp_path / 'other')
    assert _call(other, 'business_context.rules.preview', {'rule_id': created['rule_id']})[0] == 404


# LLM: 原生管理员门、功能开关与live check三者均不可被数据字段绕过。
# 函数用途: 验证非管理员和已撤权上下文不产生策略记录。
def test_admin_owner_and_live_access_boundaries(tmp_path):
    ordinary = _host(tmp_path, admin=False)
    for action, params, revision in (
        ('autonomy_policy.update', {'action': 'service', 'level': 'shadow'}, 0),
        ('autonomy_policy.reset', {'action': 'service'}, 0),
        ('autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True}, 0),
        ('autonomy_policy.grants.create', {'action': 'service', 'level': 'shadow'}, None),
        ('autonomy_policy.grants.revoke', {'grant_id': 'missing'}, None),
    ):
        result = _call(ordinary, action, params, key='ordinary-admin-denied', revision=revision)
        assert result[0] == 403, result
    assert not (ordinary.home_paths.owner_home_dir / 'data' / 'business_v13').exists()
    result = _call(ordinary, 'autonomy_policy.list', authorize=lambda action, facts: False)
    assert result[0] == 403 and result[1]['error']['code'] == 'CAPABILITY_DENIED'
    ordinary.config.business_spaces_enabled = False
    assert _call(ordinary, 'autonomy_policy.list')[0] == 503


# LLM: 同一清单应包含所有50动作，闭合schema必须在任何领域调用前拒绝未知字段。
# 函数用途: 验证W11静态目录、权限位与Schema引用闭包。
def test_registry_contracts_closed_and_complete(tmp_path):
    from agent_py_agent.agent.gateway_parts.business_api_v13_w11 import (
        _ACTION_CONTRACTS,
        V13_ACTIONS,
        V13_COLLECTIONS,
        V13_DEFS,
        V13_JOB_KINDS,
    )
    # 经冻结合同逐字段核验后的静态投影指纹；不依赖部署外任务文件。
    for value, expected in ((_ACTION_CONTRACTS, 'd69fce5f0c53d63e77a3c6afdbfcd46033b0536c8d599aeac459e2a0f2ea21c4'),
                            (V13_DEFS, '3fd49da67b0f0955d66f0533f6eff0b09ab20c2f6fb5eed57610e7404020e9fc')):
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        assert hashlib.sha256(encoded).hexdigest() == expected
    assert len(V13_ACTIONS) == 50
    assert len({item.name for item in V13_ACTIONS}) == 50
    assert V13_COLLECTIONS['conversations'] == V13_COLLECTIONS['soc_alerts'] == 'owner'
    assert set(V13_JOB_KINDS) == {'skill_backtest'}
    host = _host(tmp_path)
    for item in V13_ACTIONS:
        request = {'action': item.name, 'request_id': 'closed-check', 'params': {'unexpected_field': True}}
        if item.idempotency_required:
            request['idempotency_key'] = 'closed-schema-key'
        if item.expected_revision_required:
            request['expected_revision'] = 0
        context = api.bind_business_context(host, authorize=lambda action, facts: True)
        status, body = api.execute_business(request, context, api.BusinessServices(host))
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', (item.name, body)
        assert v13_action_spec(item.name).result_schema


# LLM: 实际GatewayHTTPServer消费标准JSON，而非前后端各造一个假信封。
# 函数用途: 验证真实网络入口的建议读取、规则写入与标准响应。
def test_envelopes(native_http):
    for action, params in (('autonomy_policy.list', {}), ('autonomy_policy.shadow_mode.get', {}),
                           ('autonomy_policy.grants.list', {}), ('autonomy_policy.agreement', {'window': '7d'}),
                           ('business_context.rules.list', {})):
        status, _, raw = native_request(native_http, 'POST', '/client/business',
            {'action': action, 'request_id': 'wire-' + action, 'params': params})
        data = _completed((status, json.loads(raw)), action)
        if action == 'autonomy_policy.list':
            assert data['enforcement'] == 'advisory'
    rule = {'name': 'wire规则', 'match': {'entity_type': 'host', 'pattern': 'core-*'},
            'effect': {'severity_boost': 1, 'priority_label': None}}
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.create', 'request_id': 'wire-rule-create', 'params': rule,
         'idempotency_key': 'wire-context-create'})
    created = _completed((status, json.loads(raw)), 'business_context.rules.create')['rule']
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.preview', 'request_id': 'wire-preview',
         'params': {'rule_id': created['rule_id']}})
    assert _completed((status, json.loads(raw)), 'business_context.rules.preview')['matched_alerts'] == 0
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'business_context.rules.preview', 'request_id': 'wire-other-owner',
         'params': {'rule_id': created['rule_id']}}, user='business-bob')
    assert status == 404 and json.loads(raw)['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 时钟在mutate中撤回管理员身份，确定性覆盖提交前而不是入口前撤权。
# 函数用途: 验证原生管理员资格撤回后不留下策略或审计记录。
def test_admin_revocation_at_commit(tmp_path):
    host = _host(tmp_path)
    current = {'admin': True}
    host.business_native_admin = lambda: current['admin']
    now = host.business_v13_clock()

    # LLM: 仅撤销管理员资格，普通上下文依旧有效，避免用普通撤权掩盖缺口。
    # 函数用途: 在事务构建时间触发身份变化。
    def clock():
        current['admin'] = False
        return now

    host.business_v13_clock = clock
    result = _call(host, 'autonomy_policy.update', {'action': 'triage', 'level': 'shadow'},
                   key='revoke-admin-last', revision=0)
    assert result[0] == 403, result
    store = V13Store(host.home_paths.owner_home_dir, owner_id=host.home_paths.owner_id)
    assert store.list('autonomy_policies') == []
    assert store.list('audit_log') == []


# LLM: 审核窗口不能被额外的告警创建时间过滤，时间偏移须按实际时刻排序。
# 函数用途: 验证老告警的新审核及多时区反馈的正确一致率。
def test_agreement_review_window_and_timezone_order(tmp_path):
    host = _host(tmp_path)
    _seed(host, 'soc_alerts', {'old': _alert(host, 'old', created_at='2026-10-20T00:00:00+00:00')})
    earlier = _feedback(host, 'f1', 'old', 'false_positive', '2026-11-01T10:00:00+02:00')
    later = _feedback(host, 'f2', 'old', 'false_positive', '2026-11-01T09:00:00+00:00')
    later['original_verdict'] = 'false_positive'
    _seed(host, 'soc_feedback', {'f1': earlier, 'f2': later})
    result = _completed(_call(host, 'autonomy_policy.agreement', {'window': '7d'}), 'autonomy_policy.agreement')
    assert (result['human_reviewed'], result['agreed'], result['agreement_rate']) == (1, 0, 0.0)


# LLM: 没有后台写口的宿主不应在能力发现中承诺可受理异步回测。
# 函数用途: 验证回测readiness依赖原生异步作业能力。
def test_backtest_readiness_without_jobs(tmp_path):
    host = _host(tmp_path)
    data = _completed(_call(host, 'system.capabilities.extensions'), 'system.capabilities.extensions')
    assert 'skills.backtest' not in data['supported_actions']


# LLM: 注入实际入口内的来源异常，读与写的未知结果必须采用不同精确错误码。
# 函数用途: 验证所有自治写口的OUTCOME_UNKNOWN及读取来源失败信封。
def test_source_failures_and_unknown_mutations(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w11_autonomy as domain
    host = _host(tmp_path)

    # LLM: 模拟领域来源进入后失败，不伪造成功或吞异常。
    # 函数用途: 为同一dispatcher注入真实OSError分支。
    def unavailable(call):
        raise OSError('synthetic source unavailable')

    monkeypatch.setattr(domain, 'v13_store_for', unavailable)
    for action, params, revision in (
        ('autonomy_policy.update', {'action': 'triage', 'level': 'shadow'}, 0),
        ('autonomy_policy.reset', {'action': 'triage'}, 0),
        ('autonomy_policy.shadow_mode.set', {'alert_class': 'service', 'shadow': True}, 0),
        ('autonomy_policy.grants.create', {'action': 'triage', 'level': 'shadow'}, None),
        ('autonomy_policy.grants.revoke', {'grant_id': 'missing'}, None),
        ('business_context.rules.create', {'name': 'rule', 'match': {'entity_type': 'any', 'pattern': '*'},
                                          'effect': {'severity_boost': 1, 'priority_label': None}}, None),
    ):
        status, body = _call(host, action, params, key='unknown-mutation-key', revision=revision)
        assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', body
        assert body['state'] == 'failed' and body['data'] == {}
    status, body = _call(host, 'autonomy_policy.list')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
