"""原生监控投递、规范调度来源与真实 HTTP 告警合同。"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w8_alerts as alerts
from agent_py_agent.agent.conversation.background_delivery import (
    BackgroundDeliveryDependencies,
    record_background_response,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.agent.gateway_parts.business_api import owner_complete_host
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w8_alerts import invoke
from agent_py_agent.tests.test_v13_w8_cases import completed, rejected

native = harness.v13_native


@contextmanager
def monitor_case(server, modes=('origin',), *, user='business-alice', configure=None, separate_spaces=False):
    """用正式 W7 创建规则和镜像，再经原调度预留、准备及领取；不拼接消息元数据。"""
    sid = completed(harness.v13_call(server, 'spaces.create', {'name': '监控投递合同空间'},
        user=user, idempotency_key=uuid4().hex))['space_id']
    rules = []
    for number, _mode in enumerate(modes):
        selected_space = sid
        if number and separate_spaces:
            selected_space = completed(harness.v13_call(server, 'spaces.create', {'name': f'独立空间 {number}'},
                user=user, idempotency_key=uuid4().hex))['space_id']
        rules.append(completed(invoke(server, 'monitors.rules.create', {
            'space_id': selected_space, 'name': f'规范规则 {number}', 'severity': 'critical' if number == 0 else 'low',
            'query': {'language': 'sql', 'text': 'SELECT 1'}, 'enabled': True,
            'schedule': {'kind': 'every', 'every_seconds': 600, 'timezone': 'UTC'},
            'mitre_techniques': ['T1000'] if number == 0 else ['T1001'],
        }, user=user))['rule'])
    with ExitStack() as stack:
        agent = owner_complete_host(server[0], OwnerIdentity.provider_user('feishu', user), stack)
        repository, store, service = agent.scheduler_repository, agent.conversation_store, agent.scheduler_service
        jobs = [repository.get_job(rule['scheduler_job_id']) for rule in rules]
        for job, mode in zip(jobs, modes):
            assert job['delivery']['mode'] == 'origin'
            if mode == 'bound':
                repository.update_job(job['job_id'], expected_version=job['version'], patch={
                    'delivery': {'mode': 'bound', 'targets': [{'target_kind': 'local'}]}})
        if configure is not None:
            configure(agent, jobs)
        now = max(job['next_run_at'] for job in jobs)
        enqueued = service.enqueue_ready_runs(now=now)
        signals = {signal.wake_signal_id: signal for signal in store.wakes.pending()}
        runs, requests, claims = {}, {}, {}
        for wake_id in enqueued:
            signal = signals[wake_id]
            run_id = signal.root_task_id
            decision = service.claim_wake(signal, now=now)
            assert decision.status == 'claimed', decision
            run = repository.get_active_run(run_id)
            assert run['execution_context']['origin_thread_id']
            assert run['owner'] == repository.owner
            runs[run['job_id']], claims[run['job_id']] = run, decision.claim
            requests[run['job_id']] = BackgroundRunRequest(thread_id=run['thread_id'], task_id=run_id,
                reason=signal.reason, wake_signal=signal.to_dict())
        assert {job['job_id'] for job in jobs} <= set(runs)
        channels = DeliveryService(build_default_channel_registry(agent.config))
        yield SimpleNamespace(server=server, user=user, sid=sid, rules=rules, jobs=jobs, agent=agent,
            repository=repository, service=service, store=store, runs=runs, requests=requests, claims=claims,
            delivery=BackgroundDeliveryDependencies(agent, store, channels,
                lambda request: store.tasks.load_report(request.task_id)[0].status))


def deliver(case, index=0, *, content='规范告警\n详细证据', job=None, thread_id=None, commentaries=()):
    """原生产 sender 写出最终消息；返回值和元数据均由规范存储读回。"""
    job = job or case.jobs[index]
    run, request = case.runs[job['job_id']], case.requests[job['job_id']]
    result = record_background_response(case.delivery, request, DeliveryContext(channel='internal',
        target=run['thread_id'], thread_id=run['thread_id'], task_id=run['run_id']), content,
        deliver=True, delivery_reason='scheduled_job_due', route_supports_transcript=True,
        assistant_commentaries=commentaries)
    assert result.persisted, result
    messages, errors = case.store.messages.recent_report(thread_id or run['thread_id'], limit=1000)
    assert not errors
    matches = [message for message in messages if message.metadata.get('task_id') == run['run_id']
        and message.metadata.get('assistant_part_id') == 'final']
    assert len(matches) == 1, messages
    message = matches[0]
    assert not result.message_id or message.message_id == result.message_id
    assert message.metadata['task_id'] == run['run_id']
    return message


def alert_page(case, **params):
    return completed(invoke(case.server, 'soc.alerts.list', {'space_id': case.sid, **params}, user=case.user))


def test_native_origin_delivery_imports_without_special_routing_marker(native):
    with monitor_case(native) as case:
        message = deliver(case)
        assert 'scheduler_delivery_event_key' not in message.metadata
        page = alert_page(case)
        assert len(page['items']) == 1, page
        row = page['items'][0]
        assert (row['rule_id'], row['severity'], row['techniques']) == (
            case.rules[0]['rule_id'], 'critical', ['T1000'])
        assert row['title'] == '规范告警' and row['description'] == message.content
        assert row['source'] == 'monitor' and page['coverage'] == 'complete'


def test_native_bound_local_delivery_is_not_reassigned_by_shared_thread(native):
    with monitor_case(native, modes=('bound', 'origin')) as case:
        assert case.jobs[0]['thread_id'] == case.jobs[1]['thread_id']
        message = deliver(case)
        assert message.metadata['scheduler_delivery_event_key']
        page = alert_page(case)
        assert len(page['items']) == 1, page
        assert (page['items'][0]['rule_id'], page['items'][0]['severity'], page['items'][0]['techniques']) == (
            case.rules[0]['rule_id'], 'critical', ['T1000'])


@pytest.mark.parametrize('mode', ['origin', 'bound'])
def test_native_retry_and_repeated_reads_preserve_human_triage(native, mode):
    with monitor_case(native, modes=(mode,)) as case:
        message = deliver(case)
        first = alert_page(case)
        row = first['items'][0]
        changes = {'alert_id': row['alert_id'], 'status': 'resolved', 'verdict': 'benign',
            'assignee': 'analyst', 'tags': ['reviewed']}
        saved = completed(invoke(native, 'soc.alerts.update', changes, revision=row['revision']))['alert']
        assert deliver(case).message_id == message.message_id
        assert alert_page(case)['items'] == [saved]
        assert alert_page(case)['items'] == [saved]
        assert saved['revision'] == row['revision'] + 1 and saved['acknowledged_at'] and saved['resolved_at']
        feedback = completed(invoke(native, 'feedback.overrides.list', {'alert_id': row['alert_id']}))['items']
        assert len(feedback) == 1 and feedback[0]['override_verdict'] == 'benign'


def test_two_native_rules_share_thread_without_swallowing_either_delivery(native):
    with monitor_case(native, modes=('bound', 'origin')) as case:
        deliver(case, 0, content='规则一结果')
        deliver(case, 1, content='规则二结果')
        page = alert_page(case, limit=1)
        assert page['next_cursor'] and page['coverage'] == 'partial'
        second = alert_page(case, cursor=page['next_cursor'], limit=1)
        rows = page['items'] + second['items']
        assert {row['rule_id']: (row['title'], row['severity']) for row in rows} == {
            case.rules[0]['rule_id']: ('规则一结果', 'critical'),
            case.rules[1]['rule_id']: ('规则二结果', 'low')}
        assert len({row['alert_id'] for row in rows}) == 2
        assert second['next_cursor'] is None and second['coverage'] == 'complete'


def test_identical_results_from_distinct_native_runs_remain_distinct(native):
    with monitor_case(native) as case:
        job_id = case.jobs[0]['job_id']
        first = deliver(case)
        case.service.finish(case.claims[job_id], status='done')
        case.store.wakes.mark_handled(case.requests[job_id].wake_signal['wake_signal_id'])
        now = case.repository.get_job(job_id)['next_run_at']
        [wake_id] = case.service.enqueue_ready_runs(now=now)
        signal = case.store.wakes.pending_one(wake_id)
        decision = case.service.claim_wake(signal, now=now)
        assert decision.status == 'claimed'
        run = case.repository.get_active_run(signal.root_task_id)
        case.runs[job_id], case.claims[job_id] = run, decision.claim
        case.requests[job_id] = BackgroundRunRequest(thread_id=run['thread_id'], task_id=run['run_id'],
            reason=signal.reason, wake_signal=signal.to_dict())
        second = deliver(case)
        assert first.metadata['task_id'] != second.metadata['task_id'] and first.content == second.content
        rows = alert_page(case)['items']
        assert len(rows) == 2 and len({row['external_ref'] for row in rows}) == 2


def test_native_thousand_message_window_is_bounded_and_explicitly_partial(native, monkeypatch):
    with monitor_case(native, modes=('origin', 'origin')) as case:
        message = deliver(case, commentaries=tuple(f'采集进度 {index}' for index in range(1000)))
        rows, errors = case.store.messages.recent_report(message.thread_id, limit=1005)
        assert not errors and len(rows) == 1001
        limits = []
        original = type(case.store.messages).recent_report

        def observe(store, thread_id, *, limit=100, **options):
            if thread_id == message.thread_id:
                limits.append(limit)
            return original(store, thread_id, limit=limit, **options)

        monkeypatch.setattr(type(case.store.messages), 'recent_report', observe)
        page = alert_page(case)
        assert len(page['items']) == 1 and page['items'][0]['description'] == message.content
        assert page['coverage'] == 'partial' and page['next_cursor'] is None
        assert limits == [1000], '共享线程只能读取一次有界历史，不能逐规则重复扫描'


@pytest.mark.parametrize('status', ['done', 'failed', 'cancelled'])
def test_import_reads_terminal_canonical_history_without_regressing_it(native, status):
    with monitor_case(native) as case:
        message = deliver(case)
        job_id = case.jobs[0]['job_id']
        request = case.requests[job_id]
        case.service.finish(case.claims[job_id], status=status)
        before = case.repository.effect_run(message.metadata['task_id'])
        wake_before = case.store.wakes.pending_one(request.wake_signal['wake_signal_id']).to_dict()
        assert case.repository.get_active_run(before['run_id']) is None and before['status'] == status
        for _ in range(2):
            assert len(alert_page(case)['items']) == 1
        assert case.repository.effect_run(before['run_id']) == before
        assert case.store.wakes.pending_one(request.wake_signal['wake_signal_id']).to_dict() == wake_before


def test_ordinary_conversation_messages_do_not_become_monitor_alerts(native):
    with monitor_case(native) as case:
        tid = case.jobs[0]['thread_id']
        for role in ('user', 'assistant'):
            case.store.messages.append({'thread_id': tid, 'role': role, 'content': '普通会话内容'})
        assert alert_page(case) == {'items': [], 'next_cursor': None, 'coverage': 'complete'}


def test_missing_native_run_evidence_is_partial_not_empty_complete(native):
    with monitor_case(native) as case:
        deliver(case)
        case.service.finish(case.claims[case.jobs[0]['job_id']], status='done')
        case.repository.history_path.rename(case.repository.history_path.with_suffix('.held'))
        assert alert_page(case) == {'items': [], 'next_cursor': None, 'coverage': 'partial'}


def test_native_wrong_job_delivery_is_ignored_even_with_routing_key(native):
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest

    def configure(agent, jobs):
        agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='普通原生计划', prompt='普通检查',
            thread_id=jobs[0]['thread_id'], schedule=jobs[0]['schedule'],
            delivery={'mode': 'bound', 'targets': [{'target_kind': 'local'}]},
            now=jobs[0]['created_at']))

    with monitor_case(native, configure=configure) as case:
        [job_id] = set(case.runs) - {case.jobs[0]['job_id']}
        message = deliver(case, job=case.repository.get_job(job_id))
        assert message.metadata['scheduler_delivery_event_key']
        assert alert_page(case) == {'items': [], 'next_cursor': None, 'coverage': 'complete'}


def test_native_cross_owner_lists_and_exact_ids_stay_separate(native):
    with monitor_case(native) as alice:
        deliver(alice)
        first = alert_page(alice)['items'][0]
        assert completed(invoke(native, 'soc.alerts.list', {}, user='business-bob'))['items'] == []
        rejected(invoke(native, 'soc.alerts.get', {'alert_id': first['alert_id']}, user='business-bob'),
            404, 'RESOURCE_NOT_FOUND')
        with monitor_case(native, user='business-bob') as bob:
            deliver(bob)
            second = alert_page(bob)['items'][0]
            assert first['alert_id'] != second['alert_id'] and first['external_ref'] != second['external_ref']
            assert alert_page(alice)['items'] == [first] and alert_page(bob)['items'] == [second]


@pytest.mark.parametrize('mode', ['origin', 'bound'])
def test_only_native_final_result_not_commentary_becomes_alert(native, mode):
    with monitor_case(native, modes=(mode,)) as case:
        message = deliver(case, commentaries=('正在核对采集结果', '正在整理证据'))
        rows, errors = case.store.messages.recent_report(message.thread_id, limit=1000)
        assert not errors and len([row for row in rows if row.metadata.get('process')]) == 2
        page = alert_page(case)
        assert len(page['items']) == 1 and page['items'][0]['description'] == message.content
        assert page['items'][0]['title'] == '规范告警' and page['coverage'] == 'complete'


def test_legacy_bound_alert_retains_id_and_human_triage_on_upgrade(native):
    with monitor_case(native, modes=('bound',)) as case:
        message = deliver(case)
        key = message.metadata['scheduler_delivery_event_key']
        old_id = 'ALERT-' + hashlib.sha256(key.encode()).hexdigest()[:12].upper()
        # 旧格式集合记录来自真实原生消息，不制造 scheduler 元数据或 HTTP 响应。
        value = {'title': '规范告警', 'description': message.content, 'severity': 'critical',
            'rule_id': case.rules[0]['rule_id'], 'external_ref': key,
            'occurred_at': datetime.fromtimestamp(message.created_at, timezone.utc).isoformat()}
        context = SimpleNamespace(clock=lambda: message.created_at, owner_id=case.agent.home_paths.owner_id)
        row = alerts._new_alert(context, case.sid, value, old_id, 'monitor')
        harness.v13_seed(case.agent.home_paths.owner_home_dir, 'soc_alerts', {old_id: row})
        saved = completed(invoke(native, 'soc.alerts.update', {'alert_id': old_id, 'status': 'resolved',
            'assignee': 'original-analyst', 'verdict': 'benign'}, revision=row['revision']))['alert']
        assert alert_page(case)['items'] == [saved]
        assert alert_page(case)['items'] == [saved]


def test_legacy_key_from_another_rule_cannot_swallow_native_delivery(native):
    with monitor_case(native, modes=('bound', 'origin')) as case:
        message = deliver(case)
        key = message.metadata['scheduler_delivery_event_key']
        old_id = 'ALERT-' + hashlib.sha256(key.encode()).hexdigest()[:12].upper()
        value = {'title': '旧归属记录', 'severity': 'low', 'rule_id': case.rules[1]['rule_id'],
            'external_ref': key, 'occurred_at': datetime.fromtimestamp(message.created_at, timezone.utc).isoformat()}
        context = SimpleNamespace(clock=lambda: message.created_at, owner_id=case.agent.home_paths.owner_id)
        old = alerts._new_alert(context, case.sid, value, old_id, 'monitor')
        harness.v13_seed(case.agent.home_paths.owner_home_dir, 'soc_alerts', {old_id: old})
        rows = alert_page(case, rule_id=case.rules[0]['rule_id'])['items']
        assert len(rows) == 1 and rows[0]['alert_id'] != old_id and rows[0]['severity'] == 'critical'
        assert completed(invoke(native, 'soc.alerts.get', {'alert_id': old_id}))['alert']['rule_id'] == case.rules[1]['rule_id']


def test_actual_delivery_to_other_monitors_thread_cannot_change_rule_attribution(native):
    from agent_py_agent.agent.scheduler.delivery_policy import available_delivery_targets

    def configure(agent, jobs):
        tid = jobs[1]['thread_id']
        agent.conversation_store.threads.bind_channel({'thread_id': tid, 'channel': 'cli',
            'channel_user_id': 'local-reader', 'channel_conversation_id': 'console-monitor'})
        [binding] = [row for row in available_delivery_targets(agent) if row['thread_id'] == tid
            and row['channel'] == 'cli']
        job = jobs[0]
        agent.scheduler_repository.update_job(job['job_id'], expected_version=job['version'], patch={
            'delivery': {'mode': 'bound', 'targets': [{'target_kind': 'channel_binding',
                'binding_id': binding['binding_id']}]}})

    with monitor_case(native, modes=('origin', 'origin'), separate_spaces=True, configure=configure) as case:
        assert case.jobs[0]['thread_id'] != case.jobs[1]['thread_id']
        message = deliver(case, thread_id=case.jobs[1]['thread_id'])
        assert message.thread_id != case.runs[case.jobs[0]['job_id']]['thread_id']
        result = completed(invoke(native, 'soc.alerts.list', {}))
        assert result == {'items': [], 'next_cursor': None, 'coverage': 'complete'}


def test_projection_race_does_not_erase_later_native_triage(native, monkeypatch):
    with monitor_case(native) as case:
        deliver(case)
        paused, release = threading.Event(), threading.Event()
        original = alerts.w8_transaction
        guard = threading.Lock()
        selected = False
        active, peak = 0, 0

        def interleave(call, collections, mutate):
            nonlocal selected, active, peak
            with guard:
                wait = bool(call.params.get('projection_ids')) and not selected
                if wait:
                    selected = True
            if wait:
                paused.set()
                assert release.wait(10), '未收到投影释放事件'
            def measured(tables):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    return mutate(tables)
                finally:
                    with guard:
                        active -= 1

            return original(call, collections, measured)

        monkeypatch.setattr(alerts, 'w8_transaction', interleave)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(alert_page, case)
            try:
                assert paused.wait(10), '原生投影未到达提交边界'
                row = alert_page(case)['items'][0]
                saved = completed(invoke(native, 'soc.alerts.update', {'alert_id': row['alert_id'],
                    'status': 'resolved', 'assignee': 'race-analyst'}, revision=row['revision']))['alert']
                case.service.finish(case.claims[case.jobs[0]['job_id']], status='done')
                terminal = case.repository.effect_run(case.requests[case.jobs[0]['job_id']].task_id)
            finally:
                release.set()
            assert future.result(timeout=10)['items'] == [saved]
        assert alert_page(case)['items'] == [saved]
        assert case.repository.effect_run(terminal['run_id']) == terminal
        assert active == 0 and peak == 1


def _fd_count():
    """POSIX 数实际打开的描述符，枚举句柄在返回前关闭；不以对象数量代替内核观察。"""
    directory = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    with os.scandir(directory) as entries:
        return sum(1 for _entry in entries)


@pytest.mark.skipif(os.name == 'nt', reason='Windows 无 POSIX 描述符目录；单独的平台门核验')
@pytest.mark.parametrize('outcome', ['success', 'cancelled', 'io_error'])
def test_native_passive_projection_closes_descriptors(native, monkeypatch, record_property, outcome):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_invoke

    with monitor_case(native) as case:
        deliver(case)
        context, services = local_business_context(case.agent)

        def read():
            return v13_invoke('soc.alerts.list', {'space_id': case.sid}, context, services,
                request_id='descriptor-read')

        assert len(read()['items']) == 1
        original = type(case.repository).effect_run
        raised = False

        def once(repository, run_id):
            nonlocal raised
            if not raised and outcome != 'success':
                raised = True
                if outcome == 'cancelled':
                    raise asyncio.CancelledError('读取已取消')
                raise OSError(errno.EIO, '一次原生调度读取错误')
            return original(repository, run_id)

        monkeypatch.setattr(type(case.repository), 'effect_run', once)
        before = _fd_count()
        if outcome == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                read()
        elif outcome == 'io_error':
            with pytest.raises(OSError):
                read()
        else:
            assert len(read()['items']) == 1
        after = _fd_count()
        record_property('fd_before', before)
        record_property('fd_after', after)
        record_property('fd_delta', after - before)
        assert after - before == 0
        assert raised is (outcome != 'success')
        assert len(read()['items']) == 1


def test_single_native_source_io_error_returns_real_http_unavailable_then_recovers(native, monkeypatch):
    with monitor_case(native) as case:
        deliver(case)
        original = type(case.repository).effect_run
        failures = 0

        def once(repository, run_id):
            nonlocal failures
            if failures == 0:
                failures += 1
                raise OSError(errno.EIO, '一次原生调度读取错误')
            return original(repository, run_id)

        monkeypatch.setattr(type(case.repository), 'effect_run', once)
        rejected(invoke(native, 'soc.alerts.list', {'space_id': case.sid}), 503, 'HOST_NOT_READY')
        assert len(alert_page(case)['items']) == 1 and failures == 1
