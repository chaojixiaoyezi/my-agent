"""W7 监控规则的真实 HTTP、原生调度、耐久回执与 W8 证据回归。"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.script_store_support import delivery_scheduler
from agent_py_agent.agent.business_spaces import v13_w7_monitors as monitors
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import thaw
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests import test_business_v12_f9_e2e as execution_harness
from agent_py_agent.tests import test_v13_w0_harness as harness

v13_native = harness.v13_native
v13_host = harness.v13_host
execution_native = execution_harness.native_http


# LLM: 每条测试的 HOME 和 MY_AGENT_HOME 都隔离到 pytest 临时目录。
# 函数用途: 阻止测试读取或写入真实用户运行账。
@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'agent-home'))


# LLM: 空间、上下文和 Store 都来自原服务，只有时钟是确定性测试输入。
# 函数用途: 创建无外网的本地真实业务测试环境。
@pytest.fixture
def world(v13_host):
    host = v13_host
    host.home_paths = replace(host.home_paths, owner_id='local/main', owner_provider='local', owner_kind='main')
    host.home_paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    host.business_v13_clock = lambda: 1793664000.0
    context, services = api.local_business_context(host)
    space = services.spaces.create({'name': '监控测试空间'}, actor=services.actor(context),
                                   idempotency_key='monitor-test-space-key')
    call = v13.V13Call('monitors.rules.create', {}, 'test-monitor', services.owner_id, context.subject_id,
        context, services, services.owner_home, clock=host.business_v13_clock)
    return SimpleNamespace(host=host, context=context, services=services, sid=space['space_id'], call=call,
                           home=services.owner_home, store=V13Store(services.owner_home))


# LLM: 请求仍经真实 dispatcher 校验，不直接伪造成功信封。
# 函数用途: 用新幂等键调用本地 v13 动作。
def _local(world, action, params, *, key=None, revision=None):
    return v13.v13_invoke(action, params, world.context, world.services,
        request_id='monitors-test-request', idempotency_key=key or 'monitor-' + uuid.uuid4().hex,
        expected_revision=revision)


# LLM: 测试参数是静态数据，不驱动产品中的专用分支。
# 函数用途: 构造最小真实规则参数。
def _draft(sid=None):
    result = {'name': '固定检测规则', 'severity': 'high', 'query': {'language': 'sql', 'text': 'SELECT 1'}}
    if sid is not None:
        result['space_id'] = sid
    return result


# LLM: 规则创建经过原服务和静态 schema，不由测试手工填充产品规则。
# 函数用途: 为后续验证创建一条规则。
def _create(world, *, enabled=False, schedule=None, key=None):
    params = {**_draft(world.sid), 'enabled': enabled, 'schedule': schedule}
    return _local(world, 'monitors.rules.create', params, key=key)['rule']


# LLM: HTTP 身份经真实认证头进入，不把身份塞进 params。
# 函数用途: 调用 W0 共享 HTTP harness。
def _wire(server, action, params, *, key=None, revision=None, user='business-alice'):
    return harness.v13_call(server, action, params, user=user,
        idempotency_key=key, expected_revision=revision)


# LLM: W8 冻结字段嵌入测试，CI 不依赖任务目录、外部JSON或未同步的 W8 模块。
# 函数用途: 返回种子必须完整包含的规范记录字段。
def _seed_fields(collection):
    fields = {
        'soc_alerts': 'alert_id space_id title description severity status verdict ai_confidence disposition '
            'rule_id source external_ref entities techniques assignee case_id tags evidence_refs acknowledged_at '
            'resolved_at revision created_at updated_at owner_id',
        'soc_feedback': 'override_id alert_id space_id rule_id original_verdict override_verdict reason analyst '
            'source created_at owner_id revision updated_at',
        'soc_cases': 'case_id space_id title description severity status assignee alert_ids merged_into tags '
            'custom_values closed_at revision created_at updated_at owner_id',
        'detection_baselines': 'baseline_id space_id rule_id metric value window revision created_at updated_at owner_id',
        'assistant_agents': 'space_id owner_id agent_id purpose revision created_at updated_at',
    }
    return set(fields[collection].split())


# LLM: 共享夹具已创建真实用户空间；源记录以该真实 owner 身份写入。
# 函数用途: 取得 HTTP 测试用户的规范 owner home。
def _http_owner(native):
    return resolve_owner_home(native[1].home_paths.root,
        OwnerIdentity.provider_user('feishu', 'business-alice'))


# LLM: CRUD 主链路必须覆盖认证 HTTP、原生任务和当前版本，不只检查文件存在。
# 函数用途: 验证启用计划规则创建、读取、调度和幂等重放。
def test_create_mirrors_enabled_scheduled_rule_to_native_scheduler(v13_native):
    sid = harness.v13_space(v13_native)
    params = {**_draft(sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600, 'timezone': 'UTC'}}
    status, first = _wire(v13_native, 'monitors.rules.create', params, key='monitor-http-create-key')
    assert status == 200, first
    assert first['state'] == 'completed' and first['error'] is None
    rule = first['data']['rule']
    v13.v13_validate_result('monitors.rules.create', first['data'])
    owner = _http_owner(v13_native)
    repo = delivery_scheduler(owner.home_dir, owner.owner_id)
    jobs, errors = repo.list_jobs()
    assert errors == [] and len(jobs) == 1
    job = jobs[0]
    assert job['job_id'] == rule['scheduler_job_id'] and job['name'] == 'monitor:' + rule['rule_id']
    assert job['status'] == 'active' and job['schedule']['every_seconds'] == 600
    assistant = V13Store(owner.home_dir).get('assistant_agents', sid)
    assert job['thread_id'] == assistant['agent_id']
    assert job['source_request_id'] == 'v13-monitor-' + rule['rule_id'] + '-1'
    status, replay = _wire(v13_native, 'monitors.rules.create', params, key='monitor-http-create-key')
    assert status == 200 and replay['data'] == first['data'], replay
    assert len(repo.list_jobs()[0]) == 1
    status, fetched = _wire(v13_native, 'monitors.rules.get', {'rule_id': rule['rule_id']})
    assert status == 200 and fetched['data'] == first['data'], fetched


# LLM: 使用原v13 dispatcher和Store写入真实规则，绑定不改闭合HTTP DTO或赋予执行权。
# 函数用途: 验证v13镜像把script_id写为结构化监控依赖。
def test_native_mirror_exposes_structured_script_binding(v13_native):
    from agent_py_agent.agent.assets.monitor_adapter import get_monitor_descriptor
    sid = harness.v13_space(v13_native)
    home = _http_owner(v13_native).home_dir
    ScriptStore(home).save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(1)')
    status, body = _wire(v13_native, 'monitors.rules.create', {**_draft(sid), 'script_id': 'collect',
        'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}, key='v15-structured-ref')
    assert status == 200, body
    result = get_monitor_descriptor(home, body['data']['rule']['scheduler_job_id'])
    assert result['refs'] == [{'kind': 'script', 'id': 'collect'}]
    assert result['metadata']['alias'] == '固定检测规则'


# LLM: 使用原v13集合写路径，覆盖没有原生scheduler镜像的规则，而非手造旧版JSON集合。
# 函数用途: 验证无计划规则仍通过真实存储读口展示脚本引用。
def test_unscheduled_rule_exposes_structured_script_reference(world):
    from agent_py_agent.agent.assets.monitor_adapter import list_monitor_descriptors
    ScriptStore(world.home).save_script(AssetDescriptor(id='collect', kind='script', name='采集'), 'print(1)')
    rule = _local(world, 'monitors.rules.create', {**_draft(world.sid), 'script_id': 'collect'})['rule']
    rows = list_monitor_descriptors(world.home)
    projected = next(row for row in rows if row['id'] == rule['rule_id'])
    assert projected['refs'] == [{'kind': 'script', 'id': 'collect'}]


# LLM: 相同规则版本不能因其它规则写入而冲突，旧记录 CAS 不能更改调度。
# 函数用途: 验证更新、启停、schedule 移除/重建和最终删除的 HTTP 闭环。
def test_update_cas_and_scheduler_job_follows(v13_native):
    sid = harness.v13_space(v13_native)
    params = {**_draft(sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
    status, created = _wire(v13_native, 'monitors.rules.create', params, key='monitor-create-update-key')
    assert status == 200, created
    rule = created['data']['rule']
    other_status, other = _wire(v13_native, 'monitors.rules.create', _draft(sid), key='monitor-unrelated-rule')
    assert other_status == 200, other
    owner = _http_owner(v13_native)
    repo = delivery_scheduler(owner.home_dir, owner.owner_id)
    for patch, expected_status in [({'enabled': False}, 'paused'),
                                   ({'schedule': {'kind': 'cron', 'expression': '*/15 * * * *', 'timezone': 'UTC'}}, 'paused'),
                                   ({'enabled': True, 'name': '更新后规则'}, 'active')]:
        key = 'monitor-update-' + uuid.uuid4().hex
        old_revision = rule['revision']
        status, result = _wire(v13_native, 'monitors.rules.update', {'rule_id': rule['rule_id'], **patch},
                                key=key, revision=old_revision)
        assert status == 200, result
        rule = result['data']['rule']
        assert repo.get_job(rule['scheduler_job_id'])['status'] == expected_status
        status, replay = _wire(v13_native, 'monitors.rules.update', {'rule_id': rule['rule_id'], **patch},
                                key=key, revision=old_revision)
        assert status == 200 and replay['data'] == result['data'], replay
        before = repo.get_job(rule['scheduler_job_id'])
        status, stale = _wire(v13_native, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'enabled': False},
                               key='monitor-stale-' + uuid.uuid4().hex, revision=old_revision)
        assert status == 409 and stale['error']['code'] == 'REVISION_CONFLICT', stale
        assert repo.get_job(rule['scheduler_job_id']) == before
    job = repo.get_job(rule['scheduler_job_id'])
    assert job['schedule']['kind'] == 'cron' and '更新后规则' in job['prompt']
    old_job_id = rule['scheduler_job_id']
    status, removed = _wire(v13_native, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'schedule': None},
                            key='monitor-remove-schedule', revision=rule['revision'])
    assert status == 200 and removed['data']['rule']['scheduler_job_id'] is None, removed
    assert repo.get_job(old_job_id, include_deleted=True)['status'] == 'deleted'
    rule = removed['data']['rule']
    status, rebuilt = _wire(v13_native, 'monitors.rules.update', {'rule_id': rule['rule_id'],
        'schedule': {'kind': 'every', 'every_seconds': 900}}, key='monitor-rebuild-schedule', revision=rule['revision'])
    assert status == 200, rebuilt
    rule = rebuilt['data']['rule']
    assert len(repo.list_jobs()[0]) == 1
    status, deleted = _wire(v13_native, 'monitors.rules.delete', {'rule_id': rule['rule_id']},
                             key='monitor-delete-after-update', revision=rule['revision'])
    assert status == 200 and deleted['data']['deleted'] is True, deleted
    assert repo.list_jobs()[0] == []


# LLM: 记录删除后回执必须仍可重放，修改 key 参数必须冲突。
# 函数用途: 验证删除缺失、旧 CAS、幂等冲突与其它 owner 隔离。
def test_delete_removes_native_job_and_record(world):
    rule = _create(world)
    params = {'rule_id': rule['rule_id']}
    with pytest.raises(HostAPIError) as stale:
        _local(world, 'monitors.rules.delete', params, revision=0)
    assert stale.value.code == 'REVISION_CONFLICT'
    first = _local(world, 'monitors.rules.delete', params, key='monitor-delete-replay-key', revision=rule['revision'])
    assert _local(world, 'monitors.rules.delete', params, key='monitor-delete-replay-key', revision=rule['revision']) == first
    assert world.store.get('monitor_rules', rule['rule_id']) is None
    with pytest.raises(HostAPIError) as missing:
        _local(world, 'monitors.rules.get', params)
    assert missing.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 原始示例必须成功，分页必须绑定标签筛选与源版本。
# 函数用途: 验证 list 示例参数、所有过滤与游标篡改/过期。
def test_rules_list_example_params_and_filters(v13_native):
    sid = harness.v13_space(v13_native)
    for index in range(3):
        status, body = _wire(v13_native, 'monitors.rules.create', {**_draft(sid), 'name': '规则' + str(index),
            'tags': ['host'], 'mitre_techniques': ['T1059.001'], 'enabled': index == 0},
            key='monitor-list-create-' + str(index))
        assert status == 200, body
    status, example = _wire(v13_native, 'monitors.rules.list', {'space_id': sid, 'limit': 50})
    assert status == 200 and example['error'] is None and len(example['data']['items']) == 3, example
    filtered = {'space_id': sid, 'enabled': True, 'severity': 'high', 'tag': 'host',
                'mitre_technique': 'T1059.001', 'origin': 'manual'}
    status, body = _wire(v13_native, 'monitors.rules.list', filtered)
    assert status == 200 and len(body['data']['items']) == 1, body
    status, page = _wire(v13_native, 'monitors.rules.list', {'space_id': sid, 'limit': 1})
    assert status == 200 and page['data']['next_cursor'], page
    cursor = page['data']['next_cursor']
    for changed in [{'space_id': sid, 'limit': 1, 'tag': 'host', 'cursor': cursor},
                    {'space_id': sid, 'limit': 1, 'cursor': 'invalid'}]:
        status, error = _wire(v13_native, 'monitors.rules.list', changed)
        assert status == 503 and error['error']['code'] == 'CURSOR_EXPIRED', error


# LLM: 其它 owner 的 ID 不泄露存在信息，必须与缺记录一致。
# 函数用途: 真实 HTTP 验证跨 owner 读取为 404。
def test_rule_get_and_cross_owner_404(v13_native):
    sid = harness.v13_space(v13_native)
    status, body = _wire(v13_native, 'monitors.rules.create', _draft(sid), key='monitor-owner-create-key')
    assert status == 200, body
    rule_id = body['data']['rule']['rule_id']
    for user, rid in [('business-bob', rule_id), ('business-alice', 'absent')]:
        status, error = _wire(v13_native, 'monitors.rules.get', {'rule_id': rid}, user=user)
        assert status == 404 and error['error']['code'] == 'RESOURCE_NOT_FOUND', error


# LLM: 种子使用 W8 规定的真实字段，不发明 verdict、SLA 或观察时间字段。
# 函数用途: 创建一个规范告警事实。
def _alert(world, alert_id, rule_id, *, age=3600, severity='high', status='new'):
    at = monitors._iso(world.call.clock() - age)
    return {'alert_id': alert_id, 'space_id': world.sid, 'owner_id': world.call.owner_id,
        'title': alert_id, 'description': '', 'severity': severity, 'status': status,
        'verdict': None, 'ai_confidence': None, 'disposition': None, 'rule_id': rule_id,
        'source': 'monitor', 'external_ref': None, 'entities': [], 'techniques': [],
        'assignee': None, 'case_id': None, 'tags': [], 'evidence_refs': [],
        'acknowledged_at': None, 'resolved_at': None, 'revision': 1, 'created_at': at, 'updated_at': at}


# LLM: 最终反馈字段是 override_verdict，重复改判保留其准确时间。
# 函数用途: 创建规范 W8 反馈种子。
def _feedback(world, override_id, alert_id, rule_id, verdict, *, age=100):
    at = monitors._iso(world.call.clock() - age)
    return {'override_id': override_id, 'alert_id': alert_id, 'space_id': world.sid, 'rule_id': rule_id,
        'original_verdict': 'true_positive', 'override_verdict': verdict, 'reason': '测试反馈',
        'analyst': 'analyst', 'source': 'override', 'created_at': at, 'owner_id': world.call.owner_id,
        'revision': 1, 'updated_at': at}


# LLM: 正常种子字段集合与跨片契约逐项核验；脏记录只在专门异常测试显式注入。
# 函数用途: 经真实 V13Store 写入完整源记录。
def _seed(world, collection, records):
    required = _seed_fields(collection)
    assert all(set(row) == required for row in records.values())
    return harness.v13_seed(world.home, collection, records)


# LLM: 无目录分母时必须 null；启用与停用技术计数不得混为覆盖数。
# 函数用途: 验证 MITRE 聚合来自实际规则且不伪造目录。
def test_coverage_counts_enabled_rules_and_unknown_total(world):
    for enabled, techniques in [(True, ['T1059', 'T1059.001']), (False, ['T1059', 'T1021'])]:
        _local(world, 'monitors.rules.create', {**_draft(world.sid), 'enabled': enabled, 'mitre_techniques': techniques})
    result = _local(world, 'monitors.coverage.get', {'space_id': world.sid})
    assert result['total_known'] is None and result['coverage_ratio'] is None and result['coverage'] == 'partial'
    assert result['covered_technique_count'] == 2
    rows = {row['technique_id']: row for row in result['techniques']}
    assert len(rows['T1059']['rule_ids']) == 2 and rows['T1059']['enabled_rule_count'] == 1
    assert rows['T1021']['enabled_rule_count'] == 0 and rows['T1021']['tactic'] is None


# LLM: 无证据时 confidence=null；最新改判去重，benign 不冒充 false_positive。
# 函数用途: 验证真实反馈的关联、比例及空证据行为。
def test_confidence_from_feedback_is_null_without_evidence(world):
    rule = _create(world)
    params = {'space_id': world.sid, 'rule_id': rule['rule_id']}
    empty = _local(world, 'monitors.confidence.get', params)
    assert empty['rules'][0]['confidence'] is None and empty['coverage'] == 'unknown'
    alerts = {key: _alert(world, key, rule['rule_id']) for key in ['alert-a', 'alert-b', 'alert-c']}
    _seed(world, 'soc_alerts', alerts)
    feedback = {
        'old': _feedback(world, 'old', 'alert-a', rule['rule_id'], 'true_positive', age=200),
        'new': _feedback(world, 'new', 'alert-a', rule['rule_id'], 'false_positive'),
        'positive': _feedback(world, 'positive', 'alert-b', rule['rule_id'], 'true_positive'),
        'benign': _feedback(world, 'benign', 'alert-c', rule['rule_id'], 'benign'),
    }
    _seed(world, 'soc_feedback', feedback)
    result = _local(world, 'monitors.confidence.get', params)['rules'][0]
    assert result == {'rule_id': rule['rule_id'], 'alerts_total': 3,
                      'true_positive': 1, 'false_positive': 1, 'confidence': 0.5}
    with pytest.raises(HostAPIError) as absent:
        _local(world, 'monitors.confidence.get', {'rule_id': 'absent'})
    assert absent.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 脏字段和重复身份必须在汇总前剔除，且不能报告完整覆盖。
# 函数用途: 验证数据异常不会抬高告警数或反馈分母。
def test_metrics_exclude_missing_fields_and_duplicate_identities(world):
    rule = _create(world)
    valid = _alert(world, 'valid', rule['rule_id'])
    duplicate = _alert(world, 'duplicate', rule['rule_id'])
    malformed = {'alert_id': 'malformed', 'space_id': world.sid, 'owner_id': world.call.owner_id}
    harness.v13_seed(world.home, 'soc_alerts', {'valid': valid, 'dup-a': duplicate, 'dup-b': duplicate, 'bad': malformed})
    _seed(world, 'soc_feedback', {'feedback': _feedback(world, 'feedback', 'valid', rule['rule_id'], 'true_positive')})
    result = _local(world, 'monitors.confidence.get', {'space_id': world.sid})
    assert result['rules'][0]['alerts_total'] == 1 and result['rules'][0]['confidence'] == 1
    assert result['coverage'] == 'partial'


# LLM: 当前窗和前窗严格等长；真实基线优先，无历史证据不编造零基线。
# 函数用途: 验证告警率、历史基线、零分母与未知漂移。
def test_drift_uses_baseline_or_previous_window(world):
    rule = _create(world)
    absent = _create(world)
    alerts = {key: _alert(world, key, rule['rule_id'], age=age) for key, age in
              [('current-a', 100), ('current-b', 200), ('previous', 86400 + 1), ('future', -1)]}
    _seed(world, 'soc_alerts', alerts)
    result = _local(world, 'monitors.drift.get', {'space_id': world.sid, 'window_days': 1})
    by_rule = {row['rule_id']: row for row in result['rules']}
    assert by_rule[rule['rule_id']] == {'rule_id': rule['rule_id'], 'baseline_rate': 1,
        'current_rate': 2, 'drift_ratio': 2, 'status': 'drifting'}
    assert by_rule[absent['rule_id']]['baseline_rate'] is None
    at = monitors._iso(world.call.clock() - 10)
    baseline = {'baseline_id': 'baseline', 'owner_id': world.call.owner_id, 'space_id': world.sid,
        'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'value': 2,
        'window': {'since': monitors._iso(world.call.clock() - 86400), 'until': at},
        'revision': 1, 'created_at': at, 'updated_at': at}
    _seed(world, 'detection_baselines', {'baseline': baseline})
    rows = _local(world, 'monitors.drift.get', {'space_id': world.sid, 'window_days': 1})['rules']
    chosen = next(row for row in rows if row['rule_id'] == rule['rule_id'])
    assert chosen['baseline_rate'] == 2 and chosen['status'] == 'stable'
    _seed(world, 'detection_baselines', {'baseline': {**baseline, 'value': 0}})
    rows = _local(world, 'monitors.drift.get', {'window_days': 1})['rules']
    assert next(row for row in rows if row['rule_id'] == rule['rule_id'])['drift_ratio'] is None


# LLM: 正基线是真实独立事实，不能把被过滤告警的空集合误判为零发生率。
# 函数用途: 验证完整或部分告警被剔除后仅相关规则未知，完整规则和真实零告警不受影响。
@pytest.mark.parametrize('problem', ['missing_title', 'missing_time', 'invalid_severity', 'invalid_title', 'duplicate'])
@pytest.mark.parametrize('retain_valid', [False, True])
def test_drift_filtered_alerts_are_unknown_per_rule(world, problem, retain_valid):
    affected, healthy, empty = _create(world), _create(world), _create(world)
    broken = _alert(world, 'broken', affected['rule_id'])
    if problem == 'missing_title':
        del broken['title']
    elif problem == 'missing_time':
        del broken['created_at']
    elif problem == 'invalid_severity':
        broken['severity'] = 'invalid'
    elif problem == 'invalid_title':
        broken['title'] = {'invalid': 'object'}
    alerts = {'broken': broken, 'healthy': _alert(world, 'healthy', healthy['rule_id'])}
    if problem == 'duplicate':
        alerts['duplicate'] = dict(broken)
    if retain_valid:
        alerts['retained'] = _alert(world, 'retained', affected['rule_id'])
    harness.v13_seed(world.home, 'soc_alerts', alerts)
    at = monitors._iso(world.call.clock() - 10)
    _seed(world, 'detection_baselines', {rule['rule_id']: {
        'baseline_id': rule['rule_id'], 'owner_id': world.call.owner_id, 'space_id': world.sid,
        'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'value': 1,
        'window': {'since': monitors._iso(world.call.clock() - 86400), 'until': at},
        'revision': 1, 'created_at': at, 'updated_at': at} for rule in (affected, healthy, empty)})
    result = _local(world, 'monitors.drift.get', {'space_id': world.sid, 'window_days': 1})
    v13.v13_validate_result('monitors.drift.get', result)
    by_rule = {row['rule_id']: row for row in result['rules']}
    assert by_rule[affected['rule_id']] == {'rule_id': affected['rule_id'], 'baseline_rate': 1,
        'current_rate': None, 'drift_ratio': None, 'status': 'unknown'}
    assert by_rule[healthy['rule_id']] == {'rule_id': healthy['rule_id'], 'baseline_rate': 1,
        'current_rate': 1, 'drift_ratio': 1, 'status': 'stable'}
    assert by_rule[empty['rule_id']] == {'rule_id': empty['rule_id'], 'baseline_rate': 1,
        'current_rate': 0, 'drift_ratio': 0, 'status': 'drifting'}
    assert result['coverage'] == 'partial'


# LLM: 缺归属只能按已知范围传播；其它 owner、空间和明确无规则的告警不能污染完整规则。
# 函数用途: 验证剔除项归属不全、跨规则重复和隔离边界的漂移结果。
@pytest.mark.parametrize('problem', ['missing_rule', 'invalid_rule', 'missing_space', 'other_space',
    'other_owner', 'unassociated', 'cross_rule_duplicate'])
def test_drift_filtered_alert_gap_scope(world, problem):
    affected, healthy = _create(world), _create(world)
    other_space = world.services.spaces.create({'name': '另一监控空间'}, actor=world.services.actor(world.context),
        idempotency_key='monitor-gap-other-space')
    remote = _local(world, 'monitors.rules.create', _draft(other_space['space_id']))['rule']
    broken = _alert(world, 'broken', affected['rule_id'])
    if problem == 'missing_rule':
        del broken['rule_id']
    elif problem == 'invalid_rule':
        broken['rule_id'] = ['invalid']
    elif problem == 'missing_space':
        del broken['space_id']
    elif problem == 'other_space':
        broken['space_id'] = remote['space_id']
        del broken['title']
    elif problem == 'other_owner':
        broken['owner_id'] = 'local/another-owner'
        del broken['title']
    elif problem == 'unassociated':
        broken['rule_id'] = None
        del broken['title']
    elif problem == 'cross_rule_duplicate':
        broken['alert_id'] = 'healthy'
    alerts = {'broken': broken, 'healthy': _alert(world, 'healthy', healthy['rule_id']),
              'remote': {**_alert(world, 'remote', remote['rule_id']), 'space_id': remote['space_id']}}
    harness.v13_seed(world.home, 'soc_alerts', alerts)
    at = monitors._iso(world.call.clock() - 10)
    _seed(world, 'detection_baselines', {rule['rule_id']: {
        'baseline_id': rule['rule_id'], 'owner_id': world.call.owner_id, 'space_id': rule['space_id'],
        'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'value': 1,
        'window': {'since': monitors._iso(world.call.clock() - 86400), 'until': at},
        'revision': 1, 'created_at': at, 'updated_at': at} for rule in (affected, healthy, remote)})
    result = _local(world, 'monitors.drift.get', {'window_days': 1})
    v13.v13_validate_result('monitors.drift.get', result)
    by_rule = {row['rule_id']: row for row in result['rules']}
    unknown_ids = set()
    if problem in {'missing_rule', 'invalid_rule', 'missing_space', 'cross_rule_duplicate'}:
        unknown_ids.add(affected['rule_id'])
    if problem in {'missing_rule', 'invalid_rule', 'cross_rule_duplicate'}:
        unknown_ids.add(healthy['rule_id'])
    for rule, rate in ((affected, 0), (healthy, 1), (remote, 1)):
        unknown = rule['rule_id'] in unknown_ids
        assert by_rule[rule['rule_id']] == {'rule_id': rule['rule_id'], 'baseline_rate': 1,
            'current_rate': None if unknown else rate, 'drift_ratio': None if unknown else rate,
            'status': 'unknown' if unknown else 'stable' if rate else 'drifting'}
    assert result['coverage'] == ('complete' if problem == 'other_owner' else 'partial')


# LLM: 目标为用户实际配置；首次 CAS=0，重复严重度必须拒绝。
# 函数用途: 验证 SLA 策略的记录级 CAS、幂等和 schema。
def test_sla_set_cas(world):
    params = {'space_id': world.sid, 'policies': [{'severity': 'high', 'ack_minutes': 15, 'resolve_minutes': 120}]}
    first = _local(world, 'monitors.sla.set', params, revision=0, key='monitor-sla-create-key')
    assert first == {'policies': params['policies'], 'revision': 1}
    assert _local(world, 'monitors.sla.set', params, revision=0, key='monitor-sla-create-key') == first
    for bad_revision in (0, 2):
        with pytest.raises(HostAPIError) as stale:
            _local(world, 'monitors.sla.set', params, revision=bad_revision)
        assert stale.value.code == 'REVISION_CONFLICT'
    with pytest.raises(HostAPIError) as repeated:
        _local(world, 'monitors.sla.set', {**params, 'policies': params['policies'] * 2}, revision=1)
    assert repeated.value.code == 'INVALID_SCHEMA'


# LLM: 截止时间依赖真实 created_at/acknowledged_at/resolved_at，终态缺时间不能编造。
# 函数用途: 验证无策略未知及已配置策略的真实违约。
def test_sla_without_policy_is_unknown_and_with_policy_computes_breaches(world):
    assert _local(world, 'monitors.sla.get', {'space_id': world.sid}) == {
        'policies': [], 'breaches': [], 'coverage': 'unknown'}
    _local(world, 'monitors.sla.set', {'space_id': world.sid,
        'policies': [{'severity': 'high', 'ack_minutes': 15, 'resolve_minutes': 120}]}, revision=0)
    late = _alert(world, 'late', None, age=10800)
    ontime = _alert(world, 'ontime', None, age=10800, status='resolved')
    ontime.update(acknowledged_at=monitors._iso(world.call.clock() - 10700),
                  resolved_at=monitors._iso(world.call.clock() - 10000))
    _seed(world, 'soc_alerts', {'late': late, 'ontime': ontime})
    at = monitors._iso(world.call.clock() - 18000)
    case = {'case_id': 'case-a', 'space_id': world.sid, 'title': '事件', 'description': '',
        'severity': 'high', 'status': 'open', 'assignee': None, 'alert_ids': [], 'merged_into': None,
        'tags': [], 'custom_values': {}, 'closed_at': None, 'revision': 1, 'created_at': at,
        'updated_at': at, 'owner_id': world.call.owner_id}
    _seed(world, 'soc_cases', {'case-a': case})
    result = _local(world, 'monitors.sla.get', {'space_id': world.sid})
    assert {(row['alert_id'], row['case_id'], row['stage']) for row in result['breaches']} == {
        ('late', None, 'ack'), ('late', None, 'resolve'), (None, 'case-a', 'resolve')}
    assert result['coverage'] == 'complete'


# LLM: 告警规范中没有 sla_due_at，排序必须真实推导；无目标的记录排最后。
# 函数用途: 验证队列开放状态、SLA 排序、分页和策略变更使游标过期。
def test_queue_lists_open_alerts_ordered_by_sla_due(world):
    policies = [{'severity': 'high', 'ack_minutes': 10, 'resolve_minutes': 100},
                {'severity': 'critical', 'ack_minutes': 5, 'resolve_minutes': 20}]
    _local(world, 'monitors.sla.set', {'space_id': world.sid, 'policies': policies}, revision=0)
    alerts = {'high': _alert(world, 'high', None, age=100),
              'critical': _alert(world, 'critical', None, age=50, severity='critical'),
              'no-policy': _alert(world, 'no-policy', None, age=1000, severity='low'),
              'closed': _alert(world, 'closed', None, status='closed')}
    _seed(world, 'soc_alerts', alerts)
    params = {'space_id': world.sid, 'limit': 1}
    page = _local(world, 'monitors.queue.list', params)
    assert page['items'][0]['alert_id'] == 'critical'
    assert page['items'][0]['sla_due_at'] == monitors._iso(world.call.clock() + 250)
    cursor = page['next_cursor']
    second = _local(world, 'monitors.queue.list', {**params, 'cursor': cursor})
    assert second['items'][0]['alert_id'] == 'high'
    rest = _local(world, 'monitors.queue.list', {'space_id': world.sid})
    assert [row['alert_id'] for row in rest['items']] == ['critical', 'high', 'no-policy']
    assert rest['items'][-1]['sla_due_at'] is None and rest['coverage'] == 'partial'
    _local(world, 'monitors.sla.set', {'space_id': world.sid, 'policies': policies}, revision=1)
    with pytest.raises(BusinessError) as expired:
        _local(world, 'monitors.queue.list', {**params, 'cursor': cursor})
    assert expired.value.code == 'CURSOR_EXPIRED'


# LLM: 该本地调度单测显式使用合成助手引用；真实助手创建另由 HTTP 闭环覆盖。
# 函数用途: 为无模型原生调度单测种入助手关联。
def _seed_assistant(world):
    at = monitors._iso(world.call.clock())
    _seed(world, 'assistant_agents', {world.sid: {'space_id': world.sid, 'owner_id': world.call.owner_id,
        'agent_id': 'synthetic-scheduler-thread', 'purpose': 'v13', 'revision': 0,
        'created_at': at, 'updated_at': at}})


# LLM: 整批业务状态原子提交一次，另一次提交只清除同步元数据，不能逐规则拆批。
# 函数用途: 验证批量启停、独立镜像检查点与原生暂停、恢复一致。
def test_bulk_toggle_reports_per_rule_outcome(world):
    _seed_assistant(world)
    rule = _create(world, enabled=True, schedule={'kind': 'every', 'every_seconds': 600})
    repo = delivery_scheduler(world.home, world.call.owner_id)
    before = world.store.revision('monitor_rules')
    params = {'space_id': world.sid, 'rule_ids': [rule['rule_id'], 'absent', rule['rule_id']], 'enabled': False}
    first = _local(world, 'monitors.rules.bulk_toggle', params, key='monitor-bulk-disable-key')
    assert first['results'][0] == first['results'][2]
    assert first['results'][0] == {'rule_id': rule['rule_id'], 'enabled': False, 'revision': 2, 'error_code': None}
    assert first['results'][1]['error_code'] == 'RESOURCE_NOT_FOUND'
    assert repo.get_job(rule['scheduler_job_id'])['status'] == 'paused'
    assert world.store.revision('monitor_rules') == before + 2
    audits = [row for row in world.store.list('audit_log') if row['collection'] == 'monitor_rules'
              and row['revision'] > before]
    assert {row['action'] for row in audits} == {
        'monitors.rules.bulk_toggle', 'monitors.rules.bulk_toggle:scheduler-checkpoint'}
    assert len(audits) == 2
    assert _local(world, 'monitors.rules.bulk_toggle', params, key='monitor-bulk-disable-key') == first
    assert world.store.revision('monitor_rules') == before + 2
    result = _local(world, 'monitors.rules.bulk_toggle', {**params, 'enabled': True})
    assert result['results'][0]['revision'] == 3 and repo.get_job(rule['scheduler_job_id'])['status'] == 'active'
    assert len(repo.list_jobs()[0]) == 1


# LLM: 竞争写都沿真实 Store 和记录 CAS；不以线程完成顺序模拟结果。
# 函数用途: 验证同版本的并发更新只允许一个提交。
def test_concurrent_record_cas_has_one_winner(world):
    rule = _create(world)

    # LLM: 捕获结构化错误码而不解析异常文本。
    # 函数用途: 执行竞争请求并返回准确结果。
    def update(name):
        try:
            return _local(world, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'name': name},
                          revision=rule['revision'])['rule']['revision']
        except HostAPIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(update, ['并发一', '并发二']))
    assert result.count(2) == 1 and result.count('REVISION_CONFLICT') == 1
    assert world.store.get('monitor_rules', rule['rule_id'])['revision'] == 2


# LLM: 同键必须跨进程重放原回执；不能靠内存字典实现幂等。
# 函数用途: 用另一个 Python 进程重开宿主与 Store 验证创建回放。
def test_idempotency_survives_process_restart(world):
    params = _draft(world.sid)
    first = _local(world, 'monitors.rules.create', params, key='monitor-restart-key')
    program = '''import json,sys
from dataclasses import replace
from types import SimpleNamespace
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_invoke
paths=replace(home_paths(sys.argv[1]),owner_id='local/main',owner_provider='local',owner_kind='main')
host=SimpleNamespace(home_paths=paths,config=SimpleNamespace(business_spaces_enabled=True),business_v13_providers={})
context,services=api.local_business_context(host)
result=v13_invoke('monitors.rules.create',json.loads(sys.argv[2]),context,services,idempotency_key='monitor-restart-key')
print(json.dumps(result,ensure_ascii=False))
'''
    result = subprocess.run([sys.executable, '-c', program, str(world.host.home_paths.root), json.dumps(params)],
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ), capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == first
    with pytest.raises(BusinessError) as conflict:
        _local(world, 'monitors.rules.create', {**params, 'name': '改参数'}, key='monitor-restart-key')
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'
    assert len(world.store.list('monitor_rules')) == 1


# LLM: 恢复必须依赖 Store 与 Scheduler 的耐久事实，而非当前进程持有的字典。
# 函数用途: 在另一 Python 进程接续已创建原生任务但未保存镜像身份的请求。
def test_pending_scheduler_sync_survives_process_restart(world, monkeypatch):
    _seed_assistant(world)
    params = {**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
    original = monitors._mirror

    # 函数用途: 模拟原生创建成功后、检查点之前的进程中断。
    def interrupted(call, row, agent_id=None, *, deleting=False):
        original(call, row, agent_id, deleting=deleting)
        raise OSError('process-interrupted-after-native-create')

    monkeypatch.setattr(monitors, '_mirror', interrupted)
    with pytest.raises(OSError, match='process-interrupted'):
        _local(world, 'monitors.rules.create', params, key='monitor-native-restart-key')
    repo = delivery_scheduler(world.home, world.call.owner_id)
    job = repo.list_jobs()[0][0]
    pending = world.store.list('monitor_rules')[0]
    assert pending['scheduler_job_id'] is None and '_scheduler_pending' in pending
    program = '''import json,sys
from dataclasses import replace
from types import SimpleNamespace
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_invoke
paths=replace(home_paths(sys.argv[1]),owner_id='local/main',owner_provider='local',owner_kind='main')
host=SimpleNamespace(home_paths=paths,config=SimpleNamespace(business_spaces_enabled=True),business_v13_providers={})
context,services=api.local_business_context(host)
result=v13_invoke('monitors.rules.create',json.loads(sys.argv[2]),context,services,idempotency_key='monitor-native-restart-key')
print(json.dumps(result,ensure_ascii=False))
'''
    result = subprocess.run([sys.executable, '-c', program, str(world.host.home_paths.root), json.dumps(params)],
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ), capture_output=True, text=True, check=True)
    recovered = json.loads(result.stdout)['rule']
    assert recovered['scheduler_job_id'] == job['job_id']
    assert len(repo.list_jobs()[0]) == 1
    stored = world.store.get('monitor_rules', recovered['rule_id'])
    assert stored['revision'] == 1 and stored['scheduler_job_id'] == job['job_id']
    assert '_scheduler_pending' not in stored
    v13.v13_validate_result('monitors.rules.create', {'rule': recovered})


# LLM: 未到耐久提交点不得创建原生计划；同键重试仅创建一个镜像。
# 函数用途: 验证创建失败前后原生调度和权威规则一致。
def test_scheduler_creation_retry_after_store_commit_failure(world, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store as store_module
    _seed_assistant(world)
    original = store_module.write_bytes_atomic_beneath
    failures = []

    # LLM: 只注入一次真实持久化边界故障，其它原生写入不替换。
    # 函数用途: 在 monitor_rules 首个提交点前制造 I/O 失败。
    def fail_commit(root, parts, payload):
        if parts[-1] == '.transaction.json' and b'"monitor_rules"' in payload and not failures:
            failures.append('injected')
            raise OSError('injected-write-failure')
        return original(root, parts, payload)

    monkeypatch.setattr(store_module, 'write_bytes_atomic_beneath', fail_commit)
    with pytest.raises(OSError):
        _create(world, enabled=True, schedule={'kind': 'every', 'every_seconds': 600}, key='monitor-native-failure-key')
    repo = delivery_scheduler(world.home, world.call.owner_id)
    assert repo.list_jobs()[0] == []
    assert world.store.list('monitor_rules') == []
    retried = _create(world, enabled=True, schedule={'kind': 'every', 'every_seconds': 600}, key='monitor-native-failure-key')
    assert repo.list_jobs()[0][0]['job_id'] == retried['scheduler_job_id'] and len(repo.list_jobs()[0]) == 1


# LLM: 故障在真正 journal 替换前，不能把原生调度已改变当成容许的未知结果。
# 函数用途: 覆盖普通更新、删除、整批启停及组合事务的提交前调度不变量。
@pytest.mark.parametrize('operation', ['update', 'delete', 'bulk', 'combined', 'combined_create'])
def test_scheduler_unchanged_before_durable_commit(world, monkeypatch, operation):
    from agent_py_agent.agent.business_spaces import v13_store as store_module
    _seed_assistant(world)
    rule = _create(world, enabled=True, schedule={'kind': 'every', 'every_seconds': 600})
    repo = delivery_scheduler(world.home, world.call.owner_id)
    jobs_before = repo.list_jobs(include_deleted=True)
    rules_before = world.store.list('monitor_rules')
    original = store_module.write_bytes_atomic_beneath

    # 函数用途: 只拒绝本次业务事务的首次 journal 提交。
    def fail_commit(root, parts, payload):
        if parts[-1] == '.transaction.json' and b'"monitor_rules"' in payload:
            raise OSError('before-durable-commit')
        return original(root, parts, payload)

    # 函数用途: 模拟生命周期把建议与规则放进同一 Store 事务的 commit 协议。
    def combined_commit(native_mutate, extra):
        def mutate(collections):
            result = native_mutate(collections['monitor_rules'])
            collections['detection_tuning']['synthetic-suggestion'] = {'status': 'applied'}
            return result
        return monitors._store(call).transaction_many(('monitor_rules', 'detection_tuning'), mutate,
            idempotency_key=call.idempotency_key, params_digest=monitors._digest([call.params, extra]))

    call = replace(world.call, action='monitors.rules.update', params={'rule_id': rule['rule_id'], 'name': '新提示词'},
        idempotency_key='monitor-before-commit-key', expected_revision=rule['revision'])
    if operation == 'combined_create':
        call = replace(call, action='monitors.rules.create', expected_revision=None,
            params={**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}})
    monkeypatch.setattr(store_module, 'write_bytes_atomic_beneath', fail_commit)
    with pytest.raises(OSError, match='before-durable-commit'):
        if operation == 'combined_create':
            monitors.create_rule(call, origin='proposal', commit=combined_commit)
        elif operation == 'combined':
            exclusion = {'exclusion_id': 'x', 'field': 'host', 'value': 'excluded-host',
                         'reason': '测试', 'source': 'tuning'}
            monitors.update_rule(call, exclusions=[exclusion], commit=combined_commit)
        elif operation == 'bulk':
            _local(world, 'monitors.rules.bulk_toggle', {'rule_ids': [rule['rule_id']], 'enabled': False},
                key='monitor-before-commit-key')
        else:
            params = {'rule_id': rule['rule_id']}
            if operation == 'update':
                params['name'] = '新提示词'
            _local(world, 'monitors.rules.' + operation, params, revision=rule['revision'],
                key='monitor-before-commit-key')
    assert world.store.list('monitor_rules') == rules_before
    assert world.store.list('detection_tuning') == []
    assert repo.list_jobs(include_deleted=True) == jobs_before


# LLM: 原生写入前后与同步检查点三处故障均在业务耐久提交后，重试不可重复业务变更。
# 函数用途: 覆盖全部规则写路径及生命周期组合 commit 的同步恢复。
@pytest.mark.parametrize('operation', ['create', 'update', 'delete', 'bulk', 'bulk_enable',
                                      'combined_create', 'combined_update'])
@pytest.mark.parametrize('failure', ['native_before', 'native_after', 'checkpoint', 'materialize'])
def test_scheduler_recovers_after_durable_commit(world, monkeypatch, operation, failure):
    from agent_py_agent.agent.business_spaces import v13_store as store_module
    _seed_assistant(world)
    repo = delivery_scheduler(world.home, world.call.owner_id)
    creating = operation in {'create', 'combined_create'}
    rules = [] if creating else [_create(world, enabled=operation != 'bulk_enable',
        schedule={'kind': 'every', 'every_seconds': 600})]
    if operation.startswith('bulk'):
        rules.append(_create(world, enabled=operation != 'bulk_enable',
            schedule={'kind': 'every', 'every_seconds': 600}))
    params = ({**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
              if creating else {'rule_id': rules[0]['rule_id'], 'name': '已提交提示词'})
    call = replace(world.call, action='monitors.rules.create' if creating else 'monitors.rules.update',
        params=params, idempotency_key='monitor-post-commit-key', expected_revision=None if creating else 1)

    # 函数用途: 在同一提交点保存规则和合成生命周期记录，不替代 Scheduler。
    def combined_commit(native_mutate, extra):
        def mutate(collections):
            result = native_mutate(collections['monitor_rules'])
            collections['detection_tuning']['synthetic-suggestion'] = {'status': 'applied'}
            return {**result, 'combined': True}
        return monitors._store(call).transaction_many(('monitor_rules', 'detection_tuning'), mutate,
            idempotency_key=call.idempotency_key, params_digest=monitors._digest([call.params, extra]))

    # 函数用途: 同一参数和幂等键执行首次请求及故障后的重试。
    def execute():
        if creating:
            return monitors.create_rule(call, origin='proposal' if operation == 'combined_create' else 'manual',
                commit=combined_commit if operation == 'combined_create' else None)
        if operation == 'combined_update':
            exclusion = {'exclusion_id': 'x', 'field': 'host', 'value': 'excluded-host',
                         'reason': '测试', 'source': 'tuning'}
            return monitors.update_rule(call, exclusions=[exclusion], commit=combined_commit)
        if operation == 'update':
            return monitors.update_rule(call)
        if operation == 'delete':
            return _local(world, 'monitors.rules.delete', {'rule_id': rules[0]['rule_id']},
                key=call.idempotency_key, revision=1)
        return _local(world, 'monitors.rules.bulk_toggle', {'rule_ids': [row['rule_id'] for row in rules],
            'enabled': operation == 'bulk_enable'}, key=call.idempotency_key)

    original_mirror, original_write = monitors._mirror, store_module.write_bytes_atomic_beneath
    native_before = repo.list_jobs(include_deleted=True)
    faults = []

    # 函数用途: 在原生真实写入前或后注入一次失败，保留其余原生存储行为。
    def fail_native(call, row, agent_id=None, *, deleting=False):
        if failure == 'native_before' and not faults:
            faults.append('native-before')
            raise OSError('post-commit-failure')
        result = original_mirror(call, row, agent_id, deleting=deleting)
        if failure == 'native_after' and not faults:
            faults.append('native-after')
            raise OSError('post-commit-failure')
        return result

    # 函数用途: 拒绝原生同步完成后的检查点，不拒绝带待同步意图的业务提交。
    def fail_checkpoint(root, parts, payload):
        if failure == 'materialize' and parts[-1] == 'monitor_rules.json' and not faults:
            if any('_scheduler_pending' in row for row in json.loads(payload)['records'].values()):
                faults.append('materialize')
                raise OSError('post-commit-failure')
        if failure == 'checkpoint' and parts[-1] == '.transaction.json' and not faults:
            states = json.loads(payload).get('states', {})
            if 'monitor_rules' in states and not any(
                    '_scheduler_pending' in row for row in states['monitor_rules']['records'].values()):
                faults.append('checkpoint')
                raise OSError('post-commit-failure')
        return original_write(root, parts, payload)

    monkeypatch.setattr(monitors, '_mirror', fail_native)
    monkeypatch.setattr(store_module, 'write_bytes_atomic_beneath', fail_checkpoint)
    with pytest.raises(OSError, match='post-commit-failure'):
        execute()
    assert len(faults) == 1
    if failure == 'materialize':
        assert repo.list_jobs(include_deleted=True) == native_before
    committed = world.store.list('monitor_rules')
    assert any('_scheduler_pending' in row for row in committed)
    if operation.startswith('combined'):
        assert world.store.get('detection_tuning', 'synthetic-suggestion') == {'status': 'applied'}
    if operation == 'delete':
        assert committed[0]['_deleted'] is True
        with pytest.raises(HostAPIError) as absent:
            _local(world, 'monitors.rules.get', {'rule_id': rules[0]['rule_id']})
        assert absent.value.code == 'RESOURCE_NOT_FOUND'
        assert _local(world, 'monitors.rules.list', {})['items'] == []
    elif not creating:
        assert all(row['revision'] == 2 for row in committed)
    monkeypatch.setattr(monitors, '_mirror', original_mirror)
    monkeypatch.setattr(store_module, 'write_bytes_atomic_beneath', original_write)
    recovered = execute()
    jobs, errors = repo.list_jobs()
    assert errors == [] and len(jobs) == (0 if operation == 'delete' else 2 if operation.startswith('bulk') else 1)
    if operation.startswith('bulk'):
        assert all(row['status'] == ('active' if operation == 'bulk_enable' else 'paused') for row in jobs)
    elif operation != 'delete':
        assert jobs[0]['job_id'] == recovered['rule']['scheduler_job_id']
        assert jobs[0]['status'] == 'active'
        if not creating:
            assert '已提交提示词' in jobs[0]['prompt']
        if operation == 'combined_update':
            assert 'excluded-host' in jobs[0]['prompt']
        v13.v13_validate_result(call.action, {'rule': recovered['rule']})
    assert all('_scheduler_pending' not in row for row in world.store.list('monitor_rules'))
    before = repo.list_jobs(include_deleted=True)
    revision = world.store.revision('monitor_rules')
    assert execute() == recovered
    assert repo.list_jobs(include_deleted=True) == before
    assert world.store.revision('monitor_rules') == revision


# LLM: 原回执只能返回原版本，不能驱动镜像回滚或把已删除规则重新上线。
# 函数用途: 验证旧键在更新、移除计划、重建计划及删除后均不覆盖最新版本。
def test_scheduler_old_receipts_never_restore_superseded_rules(world, monkeypatch):
    _seed_assistant(world)
    params = {**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
    first = _local(world, 'monitors.rules.create', params, key='monitor-old-create')
    rule = first['rule']
    repo = delivery_scheduler(world.home, world.call.owner_id)
    patch = {'rule_id': rule['rule_id'], 'name': '旧版本提示词'}
    old = _local(world, 'monitors.rules.update', patch, revision=1, key='monitor-old-update')
    _local(world, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'schedule': None}, revision=2)
    newest = _local(world, 'monitors.rules.update', {'rule_id': rule['rule_id'], 'name': '最新提示词',
        'schedule': {'kind': 'every', 'every_seconds': 900}}, revision=3)['rule']
    assert newest['scheduler_job_id'] != rule['scheduler_job_id']
    original_mirror = monitors._mirror

    # 函数用途: 已完成请求的回放不得再触达原生可写镜像入口。
    def unexpected_mirror(call, row, agent_id=None, *, deleting=False):
        raise AssertionError('completed receipt attempted native write')

    monkeypatch.setattr(monitors, '_mirror', unexpected_mirror)
    before = repo.list_jobs(include_deleted=True)
    assert _local(world, 'monitors.rules.create', params, key='monitor-old-create') == first
    assert _local(world, 'monitors.rules.update', patch, revision=1, key='monitor-old-update') == old
    assert repo.list_jobs(include_deleted=True) == before
    assert world.store.get('monitor_rules', rule['rule_id'])['revision'] == 4
    monkeypatch.setattr(monitors, '_mirror', original_mirror)
    _local(world, 'monitors.rules.delete', {'rule_id': rule['rule_id']}, revision=4)
    before = repo.list_jobs(include_deleted=True)
    assert _local(world, 'monitors.rules.create', params, key='monitor-old-create') == first
    assert _local(world, 'monitors.rules.update', patch, revision=1, key='monitor-old-update') == old
    assert repo.list_jobs(include_deleted=True) == before and repo.list_jobs()[0] == []
    assert world.store.get('monitor_rules', rule['rule_id']) is None


# LLM: 新请求必须先收敛已提交的旧意图，再按自己的 CAS 提交，而非覆盖遗失 job_id。
# 函数用途: 验证未完成同步被后续版本接续，同键旧请求只能回放原结果。
def test_scheduler_pending_creation_is_drained_before_new_version(world, monkeypatch):
    _seed_assistant(world)
    params = {**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
    original = monitors._mirror

    # 函数用途: 保留真实原生创建结果，模拟进程在保存镜像身份前中断。
    def interrupted(call, row, agent_id=None, *, deleting=False):
        original(call, row, agent_id, deleting=deleting)
        raise OSError('after-native-create')

    monkeypatch.setattr(monitors, '_mirror', interrupted)
    with pytest.raises(OSError, match='after-native-create'):
        _local(world, 'monitors.rules.create', params, key='monitor-pending-create')
    pending = world.store.list('monitor_rules')[0]
    assert pending['scheduler_job_id'] is None and pending['revision'] == 1
    monkeypatch.setattr(monitors, '_mirror', original)
    updated = _local(world, 'monitors.rules.update', {'rule_id': pending['rule_id'], 'name': '最新规则'},
        revision=1)['rule']
    repo = delivery_scheduler(world.home, world.call.owner_id)
    before = repo.list_jobs(include_deleted=True)
    assert len(before[0]) == 1 and '最新规则' in before[0][0]['prompt']
    replay = _local(world, 'monitors.rules.create', params, key='monitor-pending-create')['rule']
    assert replay['revision'] == 1 and replay['name'] == params['name']
    assert replay['scheduler_job_id'] == updated['scheduler_job_id']
    assert repo.list_jobs(include_deleted=True) == before
    assert world.store.get('monitor_rules', pending['rule_id'])['revision'] == 2


# LLM: 存在检查针对原生资产，不扫描其它 owner，也不执行连接测试。
# 函数用途: 验证缺脚本/Skill/数据源为 404，完整引用可被保存。
def test_reference_existence_and_native_schedule_validation(world):
    for field, value in [('script_id', 'missing-script'), ('skill_ids', ['missing-skill']),
                         ('datasource_ids', ['missing-datasource'])]:
        with pytest.raises(HostAPIError) as absent:
            _local(world, 'monitors.rules.create', {**_draft(world.sid), field: value})
        assert absent.value.code == 'RESOURCE_NOT_FOUND'
    for value in [{'kind': 'every', 'every_seconds': 1}, {'kind': 'cron', 'expression': 'invalid'},
                  {'kind': 'every', 'every_seconds': 600, 'expression': '* * * * *'},
                  {'kind': 'cron', 'expression': '* * * * *', 'timezone': 'Unknown/Zone'}]:
        with pytest.raises(HostAPIError) as invalid:
            _local(world, 'monitors.rules.create', {**_draft(world.sid), 'schedule': value})
        assert invalid.value.code == 'INVALID_SCHEMA'
    with pytest.raises(HostAPIError) as technique:
        _local(world, 'monitors.rules.create', {**_draft(world.sid), 'mitre_techniques': ['T10']})
    assert technique.value.code == 'INVALID_SCHEMA'
    ScriptStore(world.home).save_script(AssetDescriptor(id='detector', kind='script', name='检测脚本'), 'print(1)')
    skill = world.home / 'skills' / 'analysis-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: analysis-skill\ndescription: 分析\n---\n只读分析。', encoding='utf-8')
    DatasourceStore(world.home).save_datasource({'datasource_id': 'source-a', 'title': '源',
                                               'kind': 'sqlite', 'database_path': ':memory:'})
    result = _local(world, 'monitors.rules.create', {**_draft(world.sid), 'script_id': 'detector',
        'skill_ids': ['analysis-skill'], 'datasource_ids': ['source-a'], 'mitre_techniques': ['T1059.001']})
    assert result['rule']['script_id'] == 'detector' and result['rule']['datasource_ids'] == ['source-a']


# LLM: 生命周期代理复用原实时调用和回执，不能靠重写存储绕过原生调度。
# 函数用途: 验证 origin 与 exclusions 的内部 API 及提示词更新。
def test_lifecycle_public_api_keeps_scheduler_in_sync(world):
    _seed_assistant(world)
    params = {**_draft(world.sid), 'enabled': True, 'schedule': {'kind': 'every', 'every_seconds': 600}}
    call = replace(world.call, params=params, idempotency_key='monitor-lifecycle-create')
    created = monitors.create_rule(call, origin='proposal')['rule']
    assert created['origin'] == 'proposal'
    exclusion = {'exclusion_id': 'exclusion-a', 'field': 'host', 'value': 'known-host',
                 'reason': '已确认范围', 'source': 'tuning'}
    call = replace(call, action='monitors.rules.update', params={'rule_id': created['rule_id']},
                   expected_revision=created['revision'], idempotency_key='monitor-lifecycle-update')
    updated = monitors.update_rule(call, exclusions=[exclusion])['rule']
    assert updated['exclusions'] == [exclusion]
    repo = delivery_scheduler(world.home, world.call.owner_id)
    assert 'known-host' in repo.get_job(updated['scheduler_job_id'])['prompt']


# LLM: 嵌入 #76-14 的职责映射，仅核静态注册边界，不声称验证 W9/W12 端点行为。
# 函数用途: 确认 #76-11 别名、跨片职责和冻结订阅身份未被 W7 注册器截获。
def test_route_map_cross_slice_entries_documented():
    from agent_py_agent.agent.gateway_parts import business_api_v13_w7 as registry
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs
    cross_slice_routes = {'GET /api/v1/fim/*': ('W9', 'fim.'),
        'GET /api/v1/honeytokens/*': ('W9', 'honeytokens.'),
        'GET /api/v1/easm/*': ('W12', 'easm.')}
    registry_names = {action.name for action in registry.V13_ACTIONS}
    assert all(owner != 'W7' and not any(action.startswith(prefix) for action in registry_names)
               for owner, prefix in cross_slice_routes.values())
    assert v13.v13_action_spec('monitors.proposals.list').action == 'detection_proposals.list'
    names = {'monitors.rules.' + name for name in ('list', 'get', 'create', 'update', 'delete', 'bulk_toggle', 'test', 'backtest')}
    names.update({'monitors.coverage.get', 'monitors.confidence.get', 'monitors.drift.get',
                  'monitors.sla.get', 'monitors.sla.set', 'monitors.queue.list'})
    registered = {name for name in registry_names if name.startswith('monitors.')}
    assert registered == names and all(v13.v13_action_spec(name).action == name for name in names)
    assert any(name.startswith('subscriptions.') for name in business_action_specs())
    assert not any(name.startswith('subscriptions.') for name in v13.v13_action_specs())


# LLM: AST 门禁防止以后把记录版本传入集合 CAS 或加入 varargs 旁路。
# 函数用途: 固定本模块的签名、注释与 CAS 边界。
def test_monitor_signature_and_record_cas_static_guard():
    path = Path(monitors.__file__)
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.args.vararg is None and node.args.kwarg is None
            assert '# 函数用途:' in '\n'.join(source.splitlines()[max(0, node.lineno - 5):node.lineno - 1])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'transaction':
            assert not any(key.arg == 'expected_revision' for key in node.keywords)
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            mirror_calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name) and node.func.id == '_mirror']
            assert not mirror_calls or function.name == '_sync_rules'


# LLM: 只替换冻结后台端口的测试响应，原 W0 job Store、状态机和结果验证完整运行。
# 函数用途: 提供可控的合成模型结果，明确不代表真实外部模型验收。
@pytest.fixture
def model_port(world, monkeypatch):
    _seed_assistant(world)
    state = {'assignments': [], 'result': {'matched': None, 'sample_matches': [], 'errors': [], 'coverage': 'unknown'}}
    original = v13.v13_call_frozen
    world.services.background = SimpleNamespace(execute=lambda: None)
    world.services.projection = SimpleNamespace(query=lambda: None)

    # LLM: 背景任务的身份与回执是合成端口输入，不把自然语言解释为执行状态。
    # 函数用途: 让真实 W0 job 逻辑读取确定性的后台完成事实与模型正文。
    def frozen(call, action, params, *, idempotency_key=None, expected_revision=None):
        if action == 'tasks.assign':
            state['assignments'].append(params)
            return DomainResult(202, {'assignment_ref': 'assignment-test', 'task_id': 'task-test',
                                      'operation_id': 'operation-test'})
        if action == 'tasks.get':
            return {'task': {'status': 'done'}}
        if action == 'messages.list':
            return {'items': [{'task_id': 'task-test', 'role': 'assistant', 'text': json.dumps(state['result'])}],
                    'next_cursor': None}
        return original(call, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)

    monkeypatch.setattr(v13, 'v13_call_frozen', frozen)
    return state


# LLM: exactly-one、异步回执、最终结果 schema 和失败结果都必须实测。
# 函数用途: 验证单次检测经 W0 作业账本完成及非法结果拒绝。
def test_rule_test_is_async_job_with_validated_result(world, model_port):
    rule = _create(world)
    before = world.store.get('monitor_rules', rule['rule_id'])
    params = {'rule_id': rule['rule_id']}
    accepted = _local(world, 'monitors.rules.test', params, key='monitor-async-test-key')
    assert isinstance(accepted, DomainResult) and accepted.http_status == 202
    data = thaw(accepted.data)
    v13.v13_validate_result('monitors.rules.test', data, accepted=True)
    observed = _local(world, 'async_jobs.get', {'job_id': data['job_id']})
    assert observed['kind'] == 'monitor_rule_test' and observed['state'] == 'completed'
    assert observed['result'] == model_port['result']
    assert world.store.get('monitor_rules', rule['rule_id']) == before
    replay = _local(world, 'monitors.rules.test', params, key='monitor-async-test-key')
    assert thaw(replay.data) == data and len(model_port['assignments']) == 1
    for invalid in [{}, {'rule_id': rule['rule_id'], 'rule': _draft()}]:
        with pytest.raises(HostAPIError) as error:
            _local(world, 'monitors.rules.test', invalid)
        assert error.value.code == 'INVALID_SCHEMA'
    model_port['result'] = {'matched': 'invented', 'sample_matches': [], 'errors': [], 'coverage': 'complete'}
    accepted = _local(world, 'monitors.rules.test', {'space_id': world.sid, 'rule': _draft()})
    observed = _local(world, 'async_jobs.get', {'job_id': thaw(accepted.data)['job_id']})
    assert observed['state'] == 'failed' and observed['error']['code'] == 'JOB_RESULT_INVALID'


# LLM: 历史窗口的两端必须有效且递增，结果必须保留同一准确窗口。
# 函数用途: 验证历史回测 kind、窗口语义及实际 W0 结果校验。
def test_backtest_job_kind_and_window_validation(world, model_port):
    rule = _create(world)
    window = {'since': monitors._iso(world.call.clock() - 86400), 'until': monitors._iso(world.call.clock())}
    model_port['result'] = {'window': window, 'evaluated_events': None, 'matches': None,
        'estimated_alerts_per_day': None, 'sample_matches': [], 'coverage': 'unknown'}
    accepted = _local(world, 'monitors.rules.backtest', {'rule_id': rule['rule_id'], 'window': window})
    assert isinstance(accepted, DomainResult) and accepted.http_status == 202
    observed = _local(world, 'async_jobs.get', {'job_id': thaw(accepted.data)['job_id']})
    assert observed['kind'] == 'monitor_rule_backtest' and observed['result']['window'] == window
    assert window['since'] in model_port['assignments'][0]['instruction']
    for invalid in [{'since': None, 'until': None}, {'since': window['until'], 'until': window['since']},
                    {'since': window['until'], 'until': window['until']}]:
        for action in ['monitors.rules.test', 'monitors.rules.backtest']:
            with pytest.raises(HostAPIError) as error:
                _local(world, action, {'rule_id': rule['rule_id'], 'window': invalid})
            assert error.value.code == 'INVALID_SCHEMA'
    with pytest.raises(HostAPIError) as missing:
        _local(world, 'monitors.rules.backtest', {'rule_id': 'absent', 'window': window})
    assert missing.value.code == 'RESOURCE_NOT_FOUND'


# LLM: HTTP 错误须保留标准失败信封，参数错误不能进入领域产生副作用。
# 函数用途: 验证 schema、引用缺失、幂等冲突及未知动作的真实运输错误码。
def test_http_error_envelopes(v13_native):
    sid = harness.v13_space(v13_native)
    rows = [('monitors.rules.create', _draft(sid), None, 400, 'INVALID_SCHEMA'),
            ('monitors.rules.create', {**_draft(sid), 'unexpected': True}, 'monitor-closed-schema', 400, 'INVALID_SCHEMA'),
            ('monitors.rules.create', {**_draft(sid), 'script_id': 'absent'}, 'monitor-missing-script', 404, 'RESOURCE_NOT_FOUND'),
            ('monitors.rules.test', {}, 'monitor-exactly-one-key', 400, 'INVALID_SCHEMA'),
            ('monitors.unknown', {}, None, 400, 'UNSUPPORTED_ACTION')]
    for action, params, key, expected_status, code in rows:
        status, body = _wire(v13_native, action, params, key=key)
        assert status == expected_status and body['error']['code'] == code, body
        assert body['state'] == 'failed' and body['data'] == {}
    status, created = _wire(v13_native, 'monitors.rules.create', _draft(sid), key='monitor-http-conflict')
    assert status == 200, created
    status, conflict = _wire(v13_native, 'monitors.rules.create', {**_draft(sid), 'name': '另一个名称'},
                             key='monitor-http-conflict')
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT', conflict


# LLM: W8 种子经真实 owner Store 写入，读取和结果校验经原生 HTTP。
# 函数用途: 覆盖所有同步指标、策略与队列的真实入口。
def test_http_metrics_sla_and_queue(v13_native):
    sid = harness.v13_space(v13_native)
    status, created = _wire(v13_native, 'monitors.rules.create', {**_draft(sid), 'enabled': True,
        'mitre_techniques': ['T1059']}, key='monitor-http-metrics-create')
    assert status == 200, created
    rule = created['data']['rule']
    owner = _http_owner(v13_native)
    now = time.time()
    source = SimpleNamespace(sid=sid, home=owner.home_dir,
        call=SimpleNamespace(owner_id=owner.owner_id, clock=lambda: now))
    _seed(source, 'soc_alerts', {'http-alert': _alert(source, 'http-alert', rule['rule_id'])})
    _seed(source, 'soc_feedback', {'http-feedback':
        _feedback(source, 'http-feedback', 'http-alert', rule['rule_id'], 'true_positive')})
    policy = {'space_id': sid, 'policies': [{'severity': 'high', 'ack_minutes': 10, 'resolve_minutes': 30}]}
    status, body = _wire(v13_native, 'monitors.sla.set', policy, key='monitor-http-sla-set', revision=0)
    assert status == 200 and body['data']['revision'] == 1, body
    for action in ['monitors.coverage.get', 'monitors.confidence.get', 'monitors.drift.get',
                   'monitors.sla.get', 'monitors.queue.list']:
        status, body = _wire(v13_native, action, {'space_id': sid})
        assert status == 200 and body['error'] is None, (action, body)
        v13.v13_validate_result(action, body['data'])
        if action == 'monitors.confidence.get':
            assert body['data']['rules'][0]['confidence'] == 1
        elif action == 'monitors.queue.list':
            assert body['data']['items'][0]['alert_id'] == 'http-alert'
            assert body['data']['items'][0]['sla_due_at'] is not None
        elif action == 'monitors.sla.get':
            assert len(body['data']['breaches']) == 2


# LLM: 后台受理需启用原生工具，使用既有 enable_tools=True/echo 夹具；不发模型外网。
# 函数用途: 验证测试/回测的真实202入站、原任务引用和jobs读取入口。
def test_http_async_acceptance_returns_real_native_task_ids(execution_native, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch
    original, diagnostics = v13.v13_call_frozen, []
    report = session_task_dispatch.runtime_error_report

    # LLM: 原始错误报告完全沿用，只补充异常被包装前的定位栈。
    # 函数用途: 为隔离环境中的原生文件系统失败保留准确来源。
    def observe_error(error, *, context=''):
        diagnostics.append({'native_error': ''.join(traceback.format_exception(error))})
        return report(error, context=context)

    monkeypatch.setattr(session_task_dispatch, 'runtime_error_report', observe_error)

    # LLM: 只记录真实异常并原样重抛，不替换端口成功、失败或任务身份。
    # 函数用途: 在运输层裁剪诊断前保留准确的冻结端口失败调用栈。
    def observe(call, action, params, *, idempotency_key=None, expected_revision=None):
        try:
            return original(call, action, params, idempotency_key=idempotency_key, expected_revision=expected_revision)
        except Exception as error:
            diagnostics.append({'action': action, 'outcome': repr(getattr(error, 'outcome', None)),
                                'traceback': ''.join(traceback.format_exception(error))})
            print(json.dumps(diagnostics, ensure_ascii=False), file=sys.stderr)
            raise

    monkeypatch.setattr(v13, 'v13_call_frozen', observe)
    sid = harness.v13_space(execution_native)
    status, created = _wire(execution_native, 'monitors.rules.create', _draft(sid), key='monitor-http-async-rule')
    assert status == 200, created
    params = {'rule_id': created['data']['rule']['rule_id']}
    window = {'since': '2026-11-01T00:00:00+00:00', 'until': '2026-11-02T00:00:00+00:00'}
    for action, request in [('monitors.rules.test', params),
                            ('monitors.rules.backtest', {**params, 'window': window})]:
        status, body = _wire(execution_native, action, request, key='monitor-http-' + action.replace('.', '-'))
        assert status == 202 and body['state'] == 'accepted', (body, diagnostics)
        data = body['data']
        assert data['job_id'] and data['task_id'] and data['operation_id']
        status, task = _wire(execution_native, 'tasks.get', {'task_id': data['task_id']})
        assert status == 200 and task['data']['task']['task_id'] == data['task_id'], task
        status, observed = _wire(execution_native, 'async_jobs.get', {'job_id': data['job_id']})
        assert status == 200 and observed['data']['task_id'] == data['task_id'], observed
        assert observed['data']['kind'] == ('monitor_rule_test' if action.endswith('.test') else 'monitor_rule_backtest')


# LLM: 对写入已入域后的真实IO异常必须报告未知结果，不伪装成功或未开始。
# 函数用途: 验证原生HTTP的 OUTCOME_UNKNOWN 和同键重试。
def test_http_commit_failure_reports_outcome_unknown(v13_native, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store as store_module
    sid = harness.v13_space(v13_native)
    original, failures = store_module.write_bytes_atomic_beneath, []

    # LLM: 故障仅针对本测试的规则提交点；不会修改业务断言或跳过失败。
    # 函数用途: 令HTTP实际经过一次持久化失败。
    def fail_commit(root, parts, payload):
        if parts[-1] == '.transaction.json' and b'"monitor_rules"' in payload and not failures:
            failures.append('io-failure')
            raise OSError('injected-http-write-failure')
        return original(root, parts, payload)

    monkeypatch.setattr(store_module, 'write_bytes_atomic_beneath', fail_commit)
    params = _draft(sid)
    status, body = _wire(v13_native, 'monitors.rules.create', params, key='monitor-http-io-retry')
    assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', body
    assert body['effect_outcome'] == 'unknown' and body['state'] == 'failed'
    status, retried = _wire(v13_native, 'monitors.rules.create', params, key='monitor-http-io-retry')
    assert status == 200 and retried['data']['rule']['revision'] == 1, retried


# LLM: 坏时间、负基线与非标量字段不进入比例，覆盖状态必须暴露资料缺口。
# 函数用途: 验证明显异常源记录的过滤与未知时间处理。
def test_drift_missing_time_and_negative_baseline_stay_unknown(world):
    rule = _create(world)
    alert = _alert(world, 'unknown-time', rule['rule_id'])
    alert['created_at'] = None
    _seed(world, 'soc_alerts', {'unknown-time': alert})
    malformed = {**_alert(world, 'bad-shape', rule['rule_id']), 'severity': {'wrong': 'object'}}
    harness.v13_seed(world.home, 'soc_alerts', {'bad-shape': malformed})
    at = monitors._iso(world.call.clock() - 10)
    baseline = {'baseline_id': 'negative', 'owner_id': world.call.owner_id, 'space_id': world.sid,
        'rule_id': rule['rule_id'], 'metric': 'alerts_per_day', 'value': -1000000,
        'window': {'since': monitors._iso(world.call.clock() - 86400), 'until': at},
        'revision': 1, 'created_at': at, 'updated_at': at}
    harness.v13_seed(world.home, 'detection_baselines', {'negative': baseline})
    result = _local(world, 'monitors.drift.get', {'space_id': world.sid, 'window_days': 1})
    assert result['rules'][0] == {'rule_id': rule['rule_id'], 'baseline_rate': None,
                                 'current_rate': None, 'drift_ratio': None, 'status': 'unknown'}
    assert result['coverage'] == 'unknown'


# LLM: 回执可复用结果但不可复用旧授权；新实时策略必须看见当前记录字段。
# 函数用途: 验证创建、批量停用和 SLA 回放在 mutate 之前重做记录级授权。
def test_receipt_replay_rechecks_live_record_authorization(world):
    params = {**_draft(world.sid), 'enabled': True, 'schedule': None}
    created = _local(world, 'monitors.rules.create', params, key='monitor-acl-create')['rule']
    bulk = {'rule_ids': [created['rule_id']], 'enabled': False}
    _local(world, 'monitors.rules.bulk_toggle', bulk, key='monitor-acl-bulk')
    sla_params = {'space_id': world.sid, 'policies': [{'severity': 'high', 'ack_minutes': 1, 'resolve_minutes': None}]}
    _local(world, 'monitors.sla.set', sla_params, key='monitor-acl-sla', revision=0)
    observed = []

    # LLM: 准入动作和参数仍允许，只撤销拥有记录事实的操作授权。
    # 函数用途: 模拟回执创建后发生的记录级权限撤销。
    def authorize(action, facts):
        if action.startswith('monitors.') and 'revision' in facts:
            observed.append(facts)
            return False
        return facts.get('owner_id', world.call.owner_id) == world.call.owner_id

    context = api.bind_business_context(world.host, authorize=authorize, subject_id=world.call.subject_id)
    for action, request, key, revision in [
            ('monitors.rules.create', params, 'monitor-acl-create', None),
            ('monitors.rules.bulk_toggle', bulk, 'monitor-acl-bulk', None),
            ('monitors.sla.set', sla_params, 'monitor-acl-sla', 0)]:
        with pytest.raises((HostAPIError, BusinessError)) as denied:
            v13.v13_invoke(action, request, context, world.services, idempotency_key=key, expected_revision=revision)
        assert denied.value.code in {'CAPABILITY_DENIED', 'BUSINESS_ACCESS_DENIED'}
    assert len(observed) == 3
    assert world.store.get('monitor_rules', created['rule_id'])['enabled'] is False
    assert world.store.get('monitor_sla_policies', world.sid)['revision'] == 1


# LLM: 可选空间只允许唯一活动候选，多个候选不能靠名称、列表顺序或创建时间猜默认值。
# 函数用途: 验证省略空间的 HTTP 写入、空空间 404 和歧义 409。
def test_optional_space_uses_unique_owner_space_only(v13_native):
    sid = harness.v13_space(v13_native)
    status, created = _wire(v13_native, 'monitors.rules.create', _draft(), key='monitor-default-space')
    assert status == 200 and created['data']['rule']['space_id'] == sid, created
    status, sla_result = _wire(v13_native, 'monitors.sla.set',
        {'policies': [{'severity': 'high', 'ack_minutes': None, 'resolve_minutes': None}]},
        key='monitor-default-sla', revision=0)
    assert status == 200 and sla_result['data']['revision'] == 1, sla_result
    status, absent = _wire(v13_native, 'monitors.rules.create', _draft(),
        key='monitor-no-space', user='business-bob')
    assert status == 404 and absent['error']['code'] == 'RESOURCE_NOT_FOUND', absent
    status, other = _wire(v13_native, 'spaces.create', {'name': '第二活动空间'}, key='monitor-second-space')
    assert status == 200, other
    status, ambiguous = _wire(v13_native, 'monitors.rules.create', _draft(), key='monitor-ambiguous-space')
    assert status == 409 and ambiguous['error']['code'] == 'STATE_CONFLICT', ambiguous
    status, rows = _wire(v13_native, 'monitors.rules.list', {})
    assert status == 200 and len(rows['data']['items']) == 1, rows
