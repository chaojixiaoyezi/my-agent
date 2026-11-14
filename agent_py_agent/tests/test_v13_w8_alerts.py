"""告警真实 Gateway 往返、原 Scheduler/消息投影及幂等/归属边界。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from agent_py_agent.agent.business_spaces import v13_w8_alerts as alerts
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w8_cases import completed, rejected, space

native = harness.v13_native


# LLM: 用户身份只走 HTTP Header，参数不接受 owner 或宿主路径。
# 函数用途: 发起真实告警动作并补齐所需信封。
def invoke(server, action, params, key=None, revision=None, user='business-alice'):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec

    if key is None and v13_action_spec(action).requires_idempotency:
        key = uuid.uuid4().hex
    return harness.v13_call(server, action, params, user=user, idempotency_key=key, expected_revision=revision)


# LLM: 仅解析 tmp_path 下本次测试身份的规范 owner 根，不猜业务存储目录。
# 函数用途: 返回测试调用者的准确空间归属。
def owner(server, user='business-alice'):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    return resolve_owner_home(server[1].home_paths.root, OwnerIdentity.provider_user('feishu', user))


# LLM: 仅用原模型管理入口配置 tmp_path 的离线 echo 档，不访问真实用户配置或网络。
# 函数用途: 为原生候选和调查提供明确可解析的合成模型档。
def echo_profile(server):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    source = server[1]
    host = SimpleNamespace(config=source.config, home_paths=home_paths_with_owner(source.home_paths, owner(server)))
    profile_id = str(uuid.uuid4())
    execute_model_profile_operation(host, 'add', {'profile_id': profile_id, 'profile': {
        'model_backend': 'echo', 'model_name': 'fixture-echo', 'transport': 'offline', 'capability': 'agentic'}})
    execute_model_profile_operation(host, 'set_default', {'profile_id': profile_id})
    return profile_id


# LLM: 仅租准确 owner 原 Agent 消费真实 task wake；只读核对原发布回执到最终规范消息，不改任务状态或输出。
# 函数用途: 在隔离测试中驱动原后台执行器完成已受理任务。
def drive_job(server, job_id):
    from contextlib import ExitStack

    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.gateway_parts.business_api import owner_complete_host
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    identity = owner(server)
    job = V13Store(identity.home_dir).get('async_jobs', job_id)
    assert job is not None and job['owner_id'] == identity.owner_id
    with ExitStack() as stack:
        agent = owner_complete_host(server[0], OwnerIdentity.provider_user('feishu', 'business-alice'), stack)
        assert agent.home_paths.owner_id == identity.owner_id
        runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store)
        scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': agent.conversation_store})
        reports = scheduler.tick_thread(job['agent_id'])
        assert reports, '原任务 wake 未被后台执行器消费'
        task = agent.conversation_store.session_tasks.require(job['task_id'])
        assert task.status in {'done', 'completed'}, task
        messages, errors = agent.conversation_store.messages.recent_report(task.target_thread_id, limit=100)
        assert not errors, errors
        assert task.conversation_request_id, '原任务必须绑定其实际执行请求'
        key = f'session_task:{task.task_id}:dispatch'
        publication = json.loads(agent.conversation_store.storage.wake_dedupe_path(
            task.target_thread_id, key).read_text(encoding='utf-8'))
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
        assert len(finals) == 1, [(message.role, message.metadata.get('dedupe_key')) for message in messages]
        transcript_id = finals[0].metadata['conversation_request_id']
        assert transcript_id and any(message.role == 'assistant'
                                     and message.metadata.get('conversation_request_id') == transcript_id
                                     and message.metadata.get('canonical_native_messages') for message in messages)


# LLM: 基线种子使用同一个真实 V13Store 事务，所有身份和值都只用于隔离测试。
# 函数用途: 构造已标记或带历史时间的规范告警。
def seed_alert(server, alert_id='alert-101', severity='high', age_days=0, verdict=None,
               confidence=None, rule_id='rule-fixture', entity=None, status='new', selected_space=None):
    home = owner(server)
    now = time.time()
    context = SimpleNamespace(clock=lambda: now, owner_id=home.owner_id)
    value = {'title': '合成告警 ' + alert_id, 'severity': severity, 'external_ref': alert_id,
        'rule_id': rule_id, 'entities': [entity or {'type': 'host', 'value': 'node-fixture'}],
        'occurred_at': datetime.fromtimestamp(now - age_days * 86400, timezone.utc).isoformat()}
    row = alerts._new_alert(context, selected_space or space(server), value, alert_id, 'ingest')
    row.update(verdict=verdict, ai_confidence=confidence, status=status)
    harness.v13_seed(home.home_dir, 'soc_alerts', {alert_id: row})
    return row


# LLM: 同外部引用跨请求、同批重复都必须去重，且不同空间不得互相吞记录。
# 函数用途: 验证 ingest 的外部引用和请求幂等。
def test_ingest_is_idempotent_by_external_ref(native):
    sid = space(native)
    item = {'external_ref': 'external-event', 'title': '外部规范事件', 'severity': 'high'}
    params = {'space_id': sid, 'alerts': [item, item]}
    key = uuid.uuid4().hex
    first = completed(invoke(native, 'soc.alerts.ingest', params, key))
    assert [row['created'] for row in first['results']] == [True, False]
    assert first['results'][0]['alert_id'] == first['results'][1]['alert_id']
    assert completed(invoke(native, 'soc.alerts.ingest', params, key)) == first
    second = completed(invoke(native, 'soc.alerts.ingest', params))
    assert not any(row['created'] for row in second['results'])
    rejected(invoke(native, 'soc.alerts.ingest', dict(params, alerts=[]), key), 409, 'IDEMPOTENCY_CONFLICT')
    assert len(completed(invoke(native, 'soc.alerts.list', {}))['items']) == 1
    rejected(invoke(native, 'soc.alerts.ingest', {'space_id': 'missing', 'alerts': [item]}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 精确 ID 不等于资源授权，另一个 owner 永远不能获得内容。
# 函数用途: 验证详情、缺失与跨身份读取。
def test_get_and_cross_owner_404(native):
    row = seed_alert(native)
    result = completed(invoke(native, 'soc.alerts.get', {'alert_id': row['alert_id']}))['alert']
    assert result['title'] == row['title'] and 'owner_id' not in result
    rejected(invoke(native, 'soc.alerts.get', {'alert_id': 'absent'}), 404, 'RESOURCE_NOT_FOUND')
    rejected(invoke(native, 'soc.alerts.get', {'alert_id': row['alert_id']}, user='business-bob'),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 读与写必须来自当前版本；重复写不重设首次时间，也不重复产生反馈。
# 函数用途: 验证状态时间戳、记录 CAS 及原子反馈。
def test_update_cas_status_timestamps(native):
    row = seed_alert(native, verdict='true_positive', confidence=0.8)
    params = {'alert_id': row['alert_id'], 'status': 'resolved', 'verdict': 'benign', 'assignee': 'lin'}
    key = uuid.uuid4().hex
    first = completed(invoke(native, 'soc.alerts.update', params, key, row['revision']))['alert']
    assert first['acknowledged_at'] and first['resolved_at'] and first['revision'] == row['revision'] + 1
    assert completed(invoke(native, 'soc.alerts.update', params, key, row['revision']))['alert'] == first
    rejected(invoke(native, 'soc.alerts.update', params, revision=row['revision']), 409, 'REVISION_CONFLICT')
    rejected(invoke(native, 'soc.alerts.update', dict(params, assignee='other'), key, row['revision']),
             409, 'IDEMPOTENCY_CONFLICT')
    final = completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'], 'status': 'new'},
        revision=first['revision']))['alert']
    assert (final['acknowledged_at'], final['resolved_at']) == (first['acknowledged_at'], first['resolved_at'])
    feedback = completed(invoke(native, 'feedback.overrides.list', {'alert_id': row['alert_id']}))['items']
    assert len(feedback) == 1 and feedback[0]['source'] == 'alert_update'
    rejected(invoke(native, 'soc.alerts.update', {'alert_id': 'absent'}, revision=0), 404, 'RESOURCE_NOT_FOUND')


# LLM: 页游标必须防篡改、跨筛选、跨身份及底层版本变化。
# 函数用途: 验证告警过滤及游标语义。
def test_list_filters_and_cursor_binding(native):
    seed_alert(native, 'a-high', 'high')
    seed_alert(native, 'b-high', 'high')
    seed_alert(native, 'c-low', 'low')
    first = completed(invoke(native, 'soc.alerts.list', {'severity': 'high', 'limit': 1}))
    assert first['next_cursor'] and len(first['items']) == 1
    cursor = first['next_cursor']
    second = completed(invoke(native, 'soc.alerts.list', {'severity': 'high', 'cursor': cursor}))
    assert first['items'][0]['alert_id'] != second['items'][0]['alert_id']
    for params in ({'severity': 'low', 'cursor': cursor}, {'cursor': cursor + 'x'}):
        rejected(invoke(native, 'soc.alerts.list', params), 410, 'CURSOR_EXPIRED')
    rejected(invoke(native, 'soc.alerts.list', {'severity': 'high', 'cursor': cursor}, user='business-bob'),
             410, 'CURSOR_EXPIRED')
    seed_alert(native, 'd-high', 'high')
    rejected(invoke(native, 'soc.alerts.list', {'severity': 'high', 'cursor': cursor}), 503, 'CURSOR_EXPIRED')


# LLM: 使用真实 scheduler job、后台线程和 canonical 消息，不手写网关响应。
# 函数用途: 验证监控投递稳定投影及多次读取不重复。
def test_list_projects_monitor_deliveries_idempotently_and_filters(native):
    from agent_py_agent.tests.test_v13_monitor_delivery_contract import deliver, monitor_case

    with monitor_case(native, modes=('bound',)) as case:
        message = deliver(case)
        job = case.jobs[0]
        source = [case.agent.home_paths.owner_id, job['job_id'], message.metadata['task_id'],
            ['event', message.metadata['scheduler_delivery_event_key']]]
        digest = hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        params = {'severity': 'critical', 'rule_id': case.rules[0]['rule_id']}
        first = completed(invoke(native, 'soc.alerts.list', params))
        assert [row['alert_id'] for row in first['items']] == ['ALERT-' + digest[:12].upper()]
        assert first['items'][0]['external_ref'] == 'monitor:' + digest
        assert first['items'][0]['title'] == '规范告警' and first['items'][0]['techniques'] == ['T1000']
        assert completed(invoke(native, 'soc.alerts.list', params)) == first
        assert completed(invoke(native, 'soc.alerts.list', {'severity': 'low'}))['items'] == []


# LLM: 不要求未合入的 W9/W7 存在；一旦存在必须核对规范 owner 的别名，不在 W8 重复注册。
# 函数用途: 验证跨切片桥接别名无歧义。
def test_soc_bridge_aliases_resolve_when_owner_slices_present():
    from agent_py_agent.agent.gateway_parts.business_api_v13 import (
        ALIASES,
        v13_action_spec,
        v13_action_specs,
    )

    specs = v13_action_specs()
    expected = {'soc.hunt.search': 'hunts.search', 'soc.hunt.saved.list': 'hunts.saved.list',
        'soc.hunt.saved.create': 'hunts.saved.create', 'soc.hunt.saved.run': 'hunts.saved.run',
        'soc.threat_intel.indicators.list': 'threat_intel.indicators.list',
        'soc.threat_intel.lookup': 'threat_intel.indicators.lookup'}
    for alias, canonical in expected.items():
        if canonical in specs:
            assert ALIASES[alias] == canonical and v13_action_spec(alias).action == canonical
        else:
            assert alias not in ALIASES


# LLM: 重建 Store 后必须读到同一持久记录，不依赖请求内存。
# 函数用途: 验证落盘、闭合信封及来源失败不冒充成功。
def test_envelopes_and_persistence_across_restart(native, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    row = seed_alert(native)
    stored = V13Store(owner(native).home_dir).get('soc_alerts', row['alert_id'])
    assert stored == row
    rejected(invoke(native, 'soc.alerts.list', {'extra': True}), 400, 'INVALID_SCHEMA')
    rejected(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id']}), 400, 'INVALID_SCHEMA')
    # LLM: 仅在隔离测试替换投影端口，显式异常必须沿真实 HTTP 失败信封返回。
    # 函数用途: 构造可确定的来源不可用错误。
    def broken(call):
        raise OSError('fixture source unavailable')
    monkeypatch.setattr(alerts, 'sync_monitor_alerts', broken)
    rejected(invoke(native, 'soc.alerts.list', {}), 503, 'HOST_NOT_READY')


# LLM: 未确认变更结局不能报完成；参数保留正常形状，仅存储回调显式异常。
# 函数用途: 验证写入失败传播为 OUTCOME_UNKNOWN。
def test_mutation_failure_maps_to_unknown(native, monkeypatch):
    def broken(call, collections, mutate):
        raise OSError('fixture write unavailable')
    row = seed_alert(native)
    monkeypatch.setattr(alerts, 'w8_transaction', broken)
    rejected(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id']}, revision=row['revision']),
             503, 'OUTCOME_UNKNOWN')


# LLM: 改绑、解绑和失败回滚都必须维护同一份双向事实，且告警只递增一次版本。
# 函数用途: 回归单条告警修改案件关联的原子性。
def test_alert_update_moves_and_detaches_case_references(native):
    from agent_py_agent.tests.test_v13_w8_cases import create

    row = seed_alert(native)
    first, second = create(native), create(native)
    bound = completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'],
        'case_id': first['case_id']}, revision=row['revision']))['alert']
    assert bound['revision'] == row['revision'] + 1
    moved = completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'],
        'case_id': second['case_id']}, revision=bound['revision']))['alert']
    assert moved['revision'] == bound['revision'] + 1
    assert completed(invoke(native, 'soc.cases.get', {'case_id': first['case_id']}))['case']['alert_ids'] == []
    assert completed(invoke(native, 'soc.cases.get', {'case_id': second['case_id']}))['case']['alert_ids'] == [row['alert_id']]
    rejected(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'], 'case_id': 'missing',
        'assignee': 'must-not-save'}, revision=moved['revision']), 404, 'RESOURCE_NOT_FOUND')
    assert completed(invoke(native, 'soc.alerts.get', {'alert_id': row['alert_id']}))['alert'] == moved
    detached = completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'],
        'case_id': None}, revision=moved['revision']))['alert']
    assert detached['case_id'] is None and detached['revision'] == moved['revision'] + 1
    assert completed(invoke(native, 'soc.cases.get', {'case_id': second['case_id']}))['case']['alert_ids'] == []
