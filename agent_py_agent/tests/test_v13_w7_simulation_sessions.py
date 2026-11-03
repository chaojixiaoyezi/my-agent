"""W7 桌面推演的真实原生 HTTP 往返与持久状态回归。"""
from __future__ import annotations

import json
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.tests import test_business_v12_f9_e2e as native_fixtures
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_mega_business_api_actions import native_request

native_http = native_fixtures.native_http
v13_host = harness.v13_host


# LLM: 本地用真实签发上下文与可撤回空间授权，时钟固定用于稳定重现并发边界。
# 函数用途: 准备空间授权与持久事务测试环境。
@pytest.fixture
def local_world(v13_host, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api as api

    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'runtime-home'))
    host = v13_host
    host.home_paths = replace(host.home_paths, owner_id='local/main', owner_provider='local', owner_kind='main')
    host.home_paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    host.business_v13_clock = lambda: 1793664000.0
    original, services = api.local_business_context(host)
    denied = set()

    # LLM: 只撤具体空间的权限，动作本身继续开放，证明回执不绕过记录复核。
    # 函数用途: 模拟宿主实时空间授权变化。
    def authorize(action, facts):
        return facts.get('space_id') not in denied and original.authorize(action, facts)

    context = api.bind_business_context(host, authorize=authorize, subject_id=original.subject_id,
        actor=original.actor, business_context=original.business_context,
        actor_factory=original.actor_factory, host_provider=original.host_provider)
    space = services.spaces.create({'name': '事务测试空间'}, actor=services.actor(context),
        idempotency_key='simulation-local-space-key')
    return SimpleNamespace(host=host, context=context, services=services, denied=denied, sid=space['space_id'])


# LLM: 直接调用仍经 v13 参数及实时权限边界，未伪造回执和领域结果。
# 函数用途: 用确定性宿主调用待测动作。
def _local(world, action, params, *, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13

    return v13.v13_invoke(action, params, world.context, world.services,
        request_id='simulation-local-request', idempotency_key=key or 'simulation-' + uuid.uuid4().hex,
        expected_revision=revision)


# LLM: 测试通过真实鉴权运输层，不替产品绕过 schema 或 owner 门。
# 函数用途: 发起原生业务请求并读取信封。
def _call(host, action, params=None, *, key=None, revision=None, user='business-alice'):
    request = {'action': action, 'request_id': 'w7-' + uuid.uuid4().hex, 'params': params or {}}
    if key is not None:
        request['idempotency_key'] = key
    if revision is not None:
        request['expected_revision'] = revision
    status, _, raw = native_request(host, 'POST', '/client/business', request, user=user)
    return status, json.loads(raw)


# LLM: 空间由冻结接口真实创建，测试不能假定一个预置空间存在。
# 函数用途: 准备本次会话所属空间。
def _space(host):
    status, body = _call(host, 'spaces.create', {'name': 'W7演练空间'}, key='w7-simulation-space-create')
    assert status == 200, body
    return body['data']['space_id']


# LLM: 辅助函数只组装有效请求，不替被测对象生成业务结果。
# 函数用途: 创建一个可继续验证的会话。
def _session(host):
    sid = _space(host)
    params = {'space_id': sid, 'scenario': 'service_recovery', 'title': '服务恢复桌面推演'}
    status, body = _call(host, 'purple_team.tabletop.create', params, key='w7-simulation-session-create')
    assert status == 200, body
    return body['data']['session']


# LLM: 原需求明确允许省略 space_id，必须由 W0 真实默认空间解析完成。
# 函数用途: 验证原始示例参数及同键重放。
def test_create_with_issue_example_params(native_http):
    _space(native_http)
    params = {'scenario': 'ransomware_lateral_movement', 'title': 'Q4 勒索横向移动桌面推演'}
    status, first = _call(native_http, 'purple_team.tabletop.create', params, key='w7-session-example-key')
    assert status == 200, first
    assert first['state'] == 'completed' and first['error'] is None
    row = first['data']['session']
    assert row['status'] == 'active' and row['findings'] == [] and row['revision'] == 1
    assert _call(native_http, 'purple_team.tabletop.create', params,
                 key='w7-session-example-key')[1]['data'] == first['data']
    status, body = _call(native_http, 'purple_team.tabletop.create',
                         {**params, 'title': 'changed'}, key='w7-session-example-key')
    assert status == 409 and body['error']['code'] == 'IDEMPOTENCY_CONFLICT', body


# LLM: 关联执行必须真实持久化，重复调用不得增加第二条发现或执行。
# 函数用途: 验证技术发现、幂等关联及读取。
def test_add_finding_creates_execution_for_technique(native_http):
    row = _session(native_http)
    params = {'session_id': row['session_id'], 'title': '检测缺口', 'severity': 'high',
              'technique_id': 'T1059', 'detected': False}
    status, first = _call(native_http, 'purple_team.tabletop.add_finding', params,
                          key='w7-simulation-finding-key', revision=row['revision'])
    assert status == 200, first
    assert first['data']['execution']['detected'] is False
    assert first['data']['session']['revision'] == row['revision'] + 1
    status, replay = _call(native_http, 'purple_team.tabletop.add_finding', params,
                           key='w7-simulation-finding-key', revision=row['revision'])
    assert status == 200 and replay['data'] == first['data'], replay
    status, fetched = _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})
    assert status == 200, fetched
    assert len(fetched['data']['session']['findings']) == 1
    assert fetched['data']['executions'] == [first['data']['execution']]


# LLM: 没技术编号的发现不应创建虚假执行，缺失和他人记录都须拒绝。
# 函数用途: 验证读取形状、404 和无技术发现。
def test_get_includes_executions(native_http):
    row = _session(native_http)
    status, result = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '沟通流程', 'severity': 'low'},
        key='w7-simulation-no-technique', revision=row['revision'])
    assert status == 200 and result['data']['execution'] is None, result
    assert _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})[1]['data']['executions'] == []
    for record_id, user in [(row['session_id'], 'business-bob'), ('absent', 'business-alice')]:
        status, body = _call(native_http, 'purple_team.tabletop.get', {'session_id': record_id}, user=user)
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body


# LLM: 归档只能推进一次，旧版本追加和新键重复完成都不能复活终态。
# 函数用途: 验证归档状态机与记录 CAS。
def test_complete_once_then_state_conflict(native_http):
    row = _session(native_http)
    params = {'session_id': row['session_id'], 'summary': '已记录问题'}
    status, first = _call(native_http, 'purple_team.tabletop.complete', params,
                          key='w7-simulation-complete', revision=row['revision'])
    assert status == 200 and first['data']['session']['status'] == 'completed', first
    assert first['data']['session']['completed_at'] is not None
    assert _call(native_http, 'purple_team.tabletop.complete', params,
                 key='w7-simulation-complete', revision=row['revision'])[1]['data'] == first['data']
    for action, args in [('purple_team.tabletop.complete', params),
                         ('purple_team.tabletop.add_finding', {'session_id': row['session_id'],
                           'title': '晚到', 'severity': 'low'})]:
        status, body = _call(native_http, action, args, key='w7-new-terminal-' + uuid.uuid4().hex,
                             revision=first['data']['session']['revision'])
        assert status == 409 and body['error']['code'] == 'STATE_CONFLICT', body
    status, stale = _call(native_http, 'purple_team.tabletop.complete', params,
                          key='w7-complete-stale-key', revision=row['revision'])
    assert status == 409 and stale['error']['code'] == 'REVISION_CONFLICT', stale


# LLM: 引用错误时不得改变执行版本，合法检测状态在同一原生入口可回读。
# 函数用途: 验证检测证据的版本与引用检查。
def test_update_detection_validates_rule_and_alert_refs(native_http):
    row = _session(native_http)
    status, added = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '验证记录', 'severity': 'medium', 'technique_id': 'T1059.001'},
        key='w7-simulation-update-prepare', revision=row['revision'])
    assert status == 200, added
    execution = added['data']['execution']
    params = {'execution_id': execution['execution_id'], 'detected': True, 'notes': '人工验证'}
    for field in ('detection_rule_id', 'alert_id'):
        status, body = _call(native_http, 'purple_team.executions.update_detection', {**params, field: 'absent'},
                             key='w7-simulation-ref-' + field, revision=execution['revision'])
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body
    status, updated = _call(native_http, 'purple_team.executions.update_detection', params,
                            key='w7-simulation-update-valid', revision=execution['revision'])
    assert status == 200 and updated['data']['execution']['detected'] is True, updated
    assert updated['data']['execution']['revision'] == execution['revision'] + 1
    assert _call(native_http, 'purple_team.tabletop.get', {'session_id': row['session_id']})[1]['data']['executions'] == [updated['data']['execution']]


# LLM: 零规则是真实计数，但没有目录时分母仍未知而不是零覆盖。
# 函数用途: 验证覆盖快照差异及幂等。
def test_drift_snapshot_records_delta_vs_previous(native_http):
    sid = _space(native_http)
    status, first = _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-first')
    assert status == 200, first
    initial = first['data']['snapshot']
    assert initial['rules_total'] == 0 and initial['coverage_ratio'] is None
    assert initial['delta_since_previous'] is None
    assert _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-first')[1]['data'] == first['data']
    status, second = _call(native_http, 'purple_team.drift_snapshot', {'space_id': sid}, key='w7-snapshot-next')
    assert status == 200, second
    assert second['data']['snapshot']['delta_since_previous'] == {
        'rules_total': 0, 'rules_enabled': 0, 'techniques_added': [], 'techniques_removed': []}


# LLM: 非法 envelope 在领域调用前拒绝，不因实现新增动作而改变冻结错误语义。
# 函数用途: 验证闭合参数、缺失 CAS 及未知动作信封。
@pytest.mark.parametrize('action,params,key,revision,code', [
    ('purple_team.tabletop.create', {'scenario': 'invalid-value', 'title': 'test'}, 'w7-invalid-schema-key', None, 'INVALID_SCHEMA'),
    ('purple_team.tabletop.complete', {'session_id': 'absent'}, 'w7-missing-revision-key', None, 'INVALID_SCHEMA'),
    ('purple_team.unknown', {}, None, None, 'UNSUPPORTED_ACTION'),
])
def test_envelopes(native_http, action, params, key, revision, code):
    status, body = _call(native_http, action, params, key=key, revision=revision)
    assert status == 400 and body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code


# LLM: 冻结合同是非空最长十六字符，不在领域层偷偷加 MITRE 专用正则。
# 函数用途: 验证自定义技术标识仍创建真实执行。
def test_custom_technique_respects_frozen_string_contract(native_http):
    row = _session(native_http)
    status, body = _call(native_http, 'purple_team.tabletop.add_finding', {
        'session_id': row['session_id'], 'title': '自定义验证', 'severity': 'low',
        'technique_id': 'custom_technique'}, key='simulation-custom-technique', revision=row['revision'])
    assert status == 200, body
    assert body['data']['execution']['technique_id'] == 'custom_technique'


# LLM: 固定时钟和反序 UUID 排除碰巧按 UUID 找到最近快照的假绿。
# 函数用途: 验证非空覆盖变化后最近提交前序可靠。
def test_snapshot_same_time_uses_commit_order(local_world, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_w7_simulation_sessions as simulation

    world = local_world
    rule = _local(world, 'monitors.rules.create', {'space_id': world.sid, 'name': '前序测试',
        'query': {'language': 'sql', 'text': 'SELECT 1'}, 'severity': 'low', 'enabled': True,
        'mitre_techniques': ['T1059']})['rule']
    ids = iter(['ffffffff', '00000000', 'aaaaaaaa'])
    monkeypatch.setattr(simulation, 'uuid', SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids))))
    _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-first')
    _local(world, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'enabled': False}, revision=rule['revision'])
    second = _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-second')
    assert second['snapshot']['delta_since_previous']['techniques_removed'] == ['T1059']
    third = _local(world, 'purple_team.drift_snapshot', {'space_id': world.sid}, key='simulation-sequence-third')
    assert third['snapshot']['delta_since_previous'] == {
        'rules_total': 0, 'rules_enabled': 0, 'techniques_added': [], 'techniques_removed': []}


# LLM: 命中回执不等于再次取得空间授权，撤权不能泄露旧结果。
# 函数用途: 验证同键重放完整保留记录级权限门。
def test_replay_rechecks_space_authorization(local_world):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    world = local_world
    row = _local(world, 'purple_team.tabletop.create', {'space_id': world.sid,
        'scenario': 'service_recovery', 'title': '撤权测试'})['session']
    params = {'session_id': row['session_id'], 'title': '一次发现', 'severity': 'low'}
    first = _local(world, 'purple_team.tabletop.add_finding', params,
        key='simulation-replay-scope', revision=row['revision'])
    assert len(first['session']['findings']) == 1
    world.denied.add(world.sid)
    with pytest.raises(HostAPIError) as error:
        _local(world, 'purple_team.tabletop.add_finding', params,
            key='simulation-replay-scope', revision=row['revision'])
    assert error.value.code == 'CAPABILITY_DENIED'
