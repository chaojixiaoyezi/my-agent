# 模块用途: ROUTE-01/02/05 与 BIZ-001 的隔离组合回归：原生 /client/business 受理 -> 原 SessionTask/唤醒 -> 真实后台调度
#   tick -> 结果/失败收尾，核对 background_agents.get / tasks.get 的结构化调度投影。只用 tmp 隔离 home 与离线 echo
#   模型档；不是线上端到端证据。
from __future__ import annotations

import http.client
import json
import time
import uuid

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, business_spaces_enabled=True,
                                    memory_path='memory.jsonl', my_agent_home=str(home)), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
                               params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


def call(native, action, params=None, **extra):
    server, agent = native
    identity = _gateway_request_identity(owner_identity_from_config(agent.config))
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=30)
    headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': identity['user_id'],
               'X-Channel': identity['channel'], 'Content-Type': 'application/json'}
    body = {'action': action, 'request_id': 'sched-' + uuid.uuid4().hex[:8], 'params': params or {}, **extra}
    try:
        conn.request('POST', '/client/business', json.dumps(body).encode(), headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def ok(native, action, params=None, **extra):
    status, payload = call(native, action, params, **extra)
    assert status == 200, (action, payload)
    return payload['data']


def add_echo_profile(agent, *, default=True):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    profile_id = str(uuid.uuid4())
    execute_model_profile_operation(agent, 'add', {'profile_id': profile_id, 'profile': {
        'model_backend': 'echo', 'model_name': 'fixture-echo', 'transport': 'offline', 'capability': 'agentic'}})
    if default:
        execute_model_profile_operation(agent, 'set_default', {'profile_id': profile_id})
    return profile_id


def assigned_task(native, name='order-worker'):
    space = ok(native, 'spaces.create', {'name': 'orders-' + name}, idempotency_key='sched-space-' + name + '-0001')
    agent = ok(native, 'background_agents.create', {'space_id': space['space_id'], 'name': name},
               idempotency_key='sched-agent-' + name + '-0001')['agent']
    receipt = ok(native, 'tasks.assign', {'agent_id': agent['agent_id'], 'space_id': space['space_id'],
                                          'instruction': 'compute order A total'},
                 idempotency_key='sched-assign-' + name + '-001')
    assert receipt['receipt_state'] == 'accepted'
    return agent, receipt['task_id']


def scheduler_for(agent):
    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=FakeDeliveryService())
    return BackgroundMainAgentScheduler({'runtime': runtime, 'store': agent.conversation_store})


def tick(scheduler, thread_id, now):
    try:
        return scheduler.tick_thread(thread_id, now=now)
    except Exception as exc:  # noqa: BLE001 - 车道异常由 Gateway 外层吞掉；这里只观察它，不当成功
        return exc


def attempt_state(agent, wake_id):
    state, error = agent.conversation_store.wakes.attempts.state_report(wake_id)
    assert error is None
    return state.to_dict()


def test_unconfigured_model_is_reported_as_structured_blocker_and_reads_consume_nothing(native):
    _, host = native
    agent, task_id = assigned_task(native)
    before = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})
    scheduling = before['scheduling']
    assert scheduling['state'] == 'model_unavailable' and scheduling['reason_code'] == 'MODEL_NOT_CONFIGURED'
    assert scheduling['model_ready'] is False and scheduling['pending_task_count'] == 1
    [wake_id] = scheduling['wake_signal_ids']
    result = tick(scheduler_for(host), agent['agent_id'], time.time())
    assert type(result).__name__ == 'ModelNotConfiguredError'
    after = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})['scheduling']
    assert after['state'] == 'model_unavailable' and after['uncounted_count'] == 1 and after['failure_count'] == 0
    assert after['last_failure_at'] and after['next_attempt_at'] and after['wake_signal_ids'] == [wake_id]
    # 只读投影：重复观察不推进唤醒、不改尝试账、不领取任务。
    ledger = attempt_state(host, wake_id)
    for _ in range(3):
        assert ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})['scheduling'] == after
        detail = ok(native, 'tasks.get', {'task_id': task_id})
        assert detail['task']['status'] == 'queued' and detail['task']['current_run_id'] is None
        assert detail['scheduling']['state'] == 'wake_backoff' and detail['scheduling']['model_ready'] is None
    assert attempt_state(host, wake_id) == ledger
    assert host.conversation_store.wakes.pending_one(wake_id) is not None


def test_configured_model_executes_and_projection_moves_from_queued_to_terminal(native):
    _, host = native
    add_echo_profile(host)
    agent, task_id = assigned_task(native)
    queued = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert queued['scheduling']['state'] == 'queued' and queued['scheduling']['model_ready'] is True
    assert queued['scheduling']['wake_signal_ids'] and queued['pending_tasks'][0]['current_run_id'] is None
    # G1/ROUTE-05：未结束的任务没有结果视图；受理不等于已结束。
    assert 'result' not in ok(native, 'tasks.get', {'task_id': task_id})
    reports = tick(scheduler_for(host), agent['agent_id'], time.time())
    assert isinstance(reports, list) and reports and reports[0].runtime_status == 'ok'
    assert host.conversation_store.session_tasks.require(task_id).status == 'done'
    idle = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert idle['scheduling']['state'] == 'no_work' and idle['pending_tasks'] == []
    detail = ok(native, 'tasks.get', {'task_id': task_id})
    assert detail['task']['status'] == 'done'
    assert detail['scheduling']['state'] == 'terminal' and detail['scheduling']['reason_code'] == ''
    # 纯文本结果经原任务耐久摘要可见；结果回报的发布状态按准确回执投影，与“已结束”分开。
    stored = host.conversation_store.session_tasks.require(task_id)
    assert stored.summary and stored.sender_thread_id and stored.result_refs == ()
    assert detail['result'] == {'summary': stored.summary, 'summary_truncated': False, 'report': 'pending'}


def test_task_result_view_redacts_bounds_and_reports_unreadable_receipts():
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_scheduling_view import task_result_view

    def unreadable(thread_id, key):
        raise OSError('wake ledger locked')

    store = SimpleNamespace(wakes=SimpleNamespace(delivery_receipt=unreadable))
    secret = 'sk-' + 'a' * 40
    done = SimpleNamespace(task_id='stask-r1', status='done', summary=f'key {secret} ' + 'x' * 5000,
                           sender_thread_id='thread-origin')
    view = task_result_view(store, done)
    assert secret not in view['summary'] and len(view['summary']) == 4000
    assert view['summary_truncated'] is True and view['report'] == 'unknown'
    receipts = SimpleNamespace(wakes=SimpleNamespace(delivery_receipt=lambda thread_id, key: {
        'session_task_result:stask-r2:failed': 'handled'}.get(key, '')))
    failed = SimpleNamespace(task_id='stask-r2', status='failed', summary='', sender_thread_id='thread-origin')
    assert task_result_view(receipts, failed) == {'summary': '', 'summary_truncated': False, 'report': 'handled'}
    unpublished = SimpleNamespace(task_id='stask-r3', status='done', summary='ok', sender_thread_id='thread-origin')
    assert task_result_view(receipts, unpublished)['report'] == 'not_published'
    orphan = SimpleNamespace(task_id='stask-r4', status='done', summary='ok', sender_thread_id='')
    assert task_result_view(receipts, orphan)['report'] == 'not_applicable'
    assert task_result_view(receipts, SimpleNamespace(task_id='stask-r5', status='pending', summary='',
                                                      sender_thread_id='thread-origin')) is None


def test_deleted_profile_counts_failures_until_quarantine_with_terminal_reason(native):
    from agent_py_agent.agent.conversation.wake_domain_closeout import SESSION_TASK_WAKE_QUARANTINED
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    _, host = native
    add_echo_profile(host)
    agent, task_id = assigned_task(native)
    doomed = add_echo_profile(host, default=False)
    model = ok(native, 'background_agents.model.get', {'agent_id': agent['agent_id']})
    ok(native, 'background_agents.model.set', {'agent_id': agent['agent_id'], 'model_profile_id': doomed},
       expected_revision=model['selection_revision'], idempotency_key='sched-model-set-doomed-01')
    execute_model_profile_operation(host, 'delete_model', {'profile_id': doomed})
    blocked = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})['scheduling']
    assert blocked['state'] == 'model_unavailable' and blocked['reason_code'] == 'MODEL_PROFILE_UNAVAILABLE'
    scheduler, start = scheduler_for(host), time.time()
    for index in range(8):
        tick(scheduler, agent['agent_id'], start + index * 400)
        if host.conversation_store.session_tasks.require(task_id).status == 'failed':
            break
        assert ok(native, 'tasks.get', {'task_id': task_id})['scheduling']['failure_count'] == index + 1
    task = host.conversation_store.session_tasks.require(task_id)
    assert task.status == 'failed' and task.failure_code == SESSION_TASK_WAKE_QUARANTINED
    detail = ok(native, 'tasks.get', {'task_id': task_id})
    assert detail['scheduling']['state'] == 'terminal'
    assert detail['scheduling']['reason_code'] == SESSION_TASK_WAKE_QUARANTINED


def test_suspended_identity_reports_paused_and_lost_wake_reports_stalled(native):
    _, host = native
    add_echo_profile(host)
    agent, task_id = assigned_task(native)
    ok(native, 'background_agents.suspend', {'agent_id': agent['agent_id']},
       expected_revision=agent['revision'], idempotency_key='sched-suspend-agent-0001')
    paused = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})['scheduling']
    assert paused['state'] == 'paused' and paused['reason_code'] == 'IDENTITY_SUSPENDED'
    other, other_task = assigned_task(native, 'second-worker')
    [wake_id] = ok(native, 'background_agents.get', {'agent_id': other['agent_id']})['scheduling']['wake_signal_ids']
    # 模拟受理后唤醒丢失（测试直接结案原唤醒）：任务仍非终态，投影必须显式报 stalled，不能显示为普通排队。
    host.conversation_store.wakes.mark_handled(wake_id)
    stalled = ok(native, 'background_agents.get', {'agent_id': other['agent_id']})['scheduling']
    assert stalled['state'] == 'stalled' and stalled['reason_code'] == 'NO_PENDING_WAKE'
    assert host.conversation_store.session_tasks.require(other_task).status == 'queued'


def test_deterministic_rejections_are_not_started_and_write_uncertainty_stays_unknown(native, monkeypatch):
    # BIZ-006：写入前的确定性拒绝报准确码与 not_started，且不改原状态；真正进入写入后的未知仍是 OUTCOME_UNKNOWN。
    _, host = native
    add_echo_profile(host)
    space = ok(native, 'spaces.create', {'name': 'p6'}, idempotency_key='sched-p6-space-00001')
    other = ok(native, 'spaces.create', {'name': 'p6b'}, idempotency_key='sched-p6-space-00002')
    agent = ok(native, 'background_agents.create', {'space_id': space['space_id'], 'name': 'w'},
               idempotency_key='sched-p6-agent-00001')['agent']
    model = ok(native, 'background_agents.model.get', {'agent_id': agent['agent_id']})
    cases = [
        ('background_agents.model.set', {'agent_id': agent['agent_id'], 'model_profile_id': str(uuid.uuid4())},
         {'expected_revision': model['selection_revision'], 'idempotency_key': 'sched-p6-model-missing'},
         404, 'MODEL_PROFILE_NOT_FOUND'),
        ('tasks.assign', {'agent_id': agent['agent_id'], 'space_id': other['space_id'], 'instruction': 'x'},
         {'idempotency_key': 'sched-p6-assign-mismatch'}, 409, 'SPACE_MISMATCH'),
        ('background_agents.model.set', {'agent_id': agent['agent_id'], 'model_profile_id': model['model_profile_id']},
         {'expected_revision': model['selection_revision'] + 7, 'idempotency_key': 'sched-p6-model-stale-1'},
         409, 'REVISION_CONFLICT'),
    ]
    for action, params, extra, status, code in cases:
        observed, payload = call(native, action, params, **extra)
        assert (observed, payload['error']['code'], payload['effect_outcome']) == (status, code, 'not_started')
        assert payload['error']['retryable'] is False
    assert ok(native, 'background_agents.model.get', {'agent_id': agent['agent_id']}) == model
    assert host.conversation_store.session_tasks.list_report(limit=0)[0] == []
    from agent_py_agent.agent.conversation import store_threads

    def broken_write(*_args, **_kwargs):
        raise OSError('disk went away mid-write')

    monkeypatch.setattr(store_threads.ThreadStore, 'update_atomic', broken_write)
    observed, payload = call(native, 'background_agents.model.set',
                             {'agent_id': agent['agent_id'], 'model_profile_id': model['model_profile_id']},
                             expected_revision=model['selection_revision'], idempotency_key='sched-p6-model-uncertain')
    assert (observed, payload['error']['code'], payload['effect_outcome']) == (503, 'OUTCOME_UNKNOWN', 'unknown')


def _cancel(native, task_id, key):
    revision = ok(native, 'tasks.get', {'task_id': task_id})['task']['revision']
    return ok(native, 'tasks.cancel', {'task_id': task_id, 'include_descendants': True},
              idempotency_key=key, expected_revision=revision)


def test_cancel_after_failed_attempt_settles_body_and_agent_is_idle_not_failed(native):
    # BIZ-008/009：一次已失败结束的尝试只留下已关闭 task_run；取消必须收口正文，身份不能永远 waiting 或显示 failed。
    _, host = native
    agent, task_id = assigned_task(native)
    assert type(tick(scheduler_for(host), agent['agent_id'], time.time())).__name__ == 'ModelNotConfiguredError'
    receipt = _cancel(native, task_id, 'sched-cancel-failed-try-1')
    assert receipt['state'] == 'completed' and receipt['outstanding_refs'] == []
    assert host.conversation_store.guidance.pending_report('thread', agent['agent_id'], limit=20) == ([], [])
    view = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert view['agent']['work_state'] == 'idle' and view['scheduling']['state'] == 'no_work'
    assert host.conversation_store.session_tasks.require(task_id).status == 'cancelled'


def test_cancel_keeps_responsibility_while_target_claim_is_running(native, monkeypatch):
    _, host = native
    agent, task_id = assigned_task(native)
    tick(scheduler_for(host), agent['agent_id'], time.time())
    claims = host.conversation_store.claims
    original = claims.load_report

    def running(thread_id):
        claim, error = original(thread_id)
        if thread_id == agent['agent_id']:
            return {**(claim or {}), 'thread_id': thread_id, 'status': 'running'}, None
        return claim, error

    monkeypatch.setattr(claims, 'load_report', running)
    receipt = _cancel(native, task_id, 'sched-cancel-while-running')
    assert receipt['state'] == 'pending' and receipt['outstanding_refs'] == [task_id]


def test_retire_with_cancel_after_failed_attempt_reaches_retired_and_releases_quota(native):
    _, host = native
    agent, task_id = assigned_task(native)
    scheduler = scheduler_for(host)
    tick(scheduler, agent['agent_id'], time.time())
    current = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})['agent']
    ok(native, 'background_agents.retire', {'agent_id': agent['agent_id'], 'disposition': 'cancel_tasks'},
       expected_revision=current['revision'], idempotency_key='sched-retire-cancel-0001')
    tick(scheduler, agent['agent_id'], time.time() + 400)
    view = ok(native, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert view['agent']['identity_state'] == 'retired' and view['scheduling']['state'] == 'retired'
    assert host.conversation_store.session_tasks.require(task_id).status == 'cancelled'
    assert ok(native, 'background_agents.list', {})['quota']['used'] == 0
