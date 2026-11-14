"""通知落本地日志不代表外送；复用真实通知端口的排队与无通道状态。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.workflows.datasource_runtime import bind_asset_im_notifier
from agent_py_agent.agent.workflows.notification_steps import (
    execute_notification_step,
    notify_unattended_run_failure,
)
from agent_py_agent.tests.test_wf_05_06_notifications_and_locks import _make_run, _make_step


@pytest.mark.parametrize('delivery', ['no_channel', 'queued'])
def test_real_native_fallback_preserves_delivery_truth_and_dedupes_queue(tmp_path, delivery):
    calls = []
    engine = SimpleNamespace(owner_home=tmp_path)
    run, step = _make_run(tmp_path), _make_step()
    def notifier(notice):
        calls.append(notice)
        return delivery
    with bind_asset_im_notifier(tmp_path, notifier):
        execute_notification_step(engine=engine, run=run, workflow=None, step=step,
            spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
        assert (tmp_path / 'workspace/runtime/notifications/im_alerts.jsonl').exists()
        if delivery == 'no_channel':
            assert step.status == 'failed'
            assert step.output_snapshot['delivery_result']['delivery_status'] == 'no_channel'
            assert 'WORKFLOW_NOTIFICATION_NO_CHANNEL' in step.error_message
            assert 'delivered_at' not in step.output_snapshot
        else:
            assert step.status == 'succeeded' and step.output_snapshot['status'] == 'queued'
            assert 'queued_at' in step.output_snapshot and 'delivered_at' not in step.output_snapshot
            execute_notification_step(engine=engine, run=run, workflow=None, step=_make_step(),
                spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
            assert len(calls) == 1


@pytest.mark.parametrize('delivery', ['no_channel', 'queued'])
def test_unattended_failure_native_fallback_does_not_claim_unsent(tmp_path, delivery):
    run = _make_run(tmp_path)
    run.status, run.failed_step_id = 'failed', 'original-step'
    run.trigger_source = 'schedule'
    with bind_asset_im_notifier(tmp_path, lambda _notice: delivery):
        result = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path), run=run)
    assert run.status == 'failed' and run.failed_step_id == 'original-step'
    assert result['status'] == ('queued' if delivery == 'queued' else 'notification_failed')
    assert result['delivery_result']['delivery_status'] == delivery
    if delivery == 'no_channel':
        assert result['error_code'] == 'WORKFLOW_NOTIFICATION_NO_CHANNEL'
    else:
        assert 'notified_at' not in result and result['queued_at']


@pytest.mark.parametrize('surface', ['step', 'run'])
@pytest.mark.parametrize('delivery', ['queued', 'no_channel'])
def test_native_delivery_preserves_local_record_failures(tmp_path, monkeypatch, surface, delivery):
    from agent_py_agent.agent.workflows import datasource_runtime, notification_steps

    sent = []
    def local_failure(*args, **kwargs):
        raise OSError('local receipt storage failed')
    monkeypatch.setattr(datasource_runtime, 'append_text_beneath', local_failure)
    original_write = notification_steps.write_text_atomic_beneath
    def post_dispatch_failure(root, parts, text):
        import json
        if json.loads(text).get('status') == 'dispatching':
            return original_write(root, parts, text)
        raise OSError('local receipt storage failed after dispatch')
    monkeypatch.setattr(notification_steps, 'write_text_atomic_beneath', post_dispatch_failure)
    engine = SimpleNamespace(owner_home=tmp_path)
    run, step = _make_run(tmp_path), _make_step()
    run.status, run.failed_step_id = 'failed', 'original-step'
    run.trigger_source = 'schedule'
    def sender(notice):
        sent.append(notice)
        return delivery
    def invoke():
        if surface == 'run':
            return notify_unattended_run_failure(engine=engine, run=run)
        execute_notification_step(engine=engine, run=run, workflow=None, step=step,
            spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
        assert step.status == ('succeeded' if delivery == 'queued' else 'failed')
        return step.output_snapshot
    with bind_asset_im_notifier(tmp_path, sender):
        receipt = invoke()
        assert receipt['delivery_result'] == {'recorded': False, 'delivery_status': delivery,
                                             'record_error': 'NOTIFICATION_STORE_UNAVAILABLE'}
        assert 'delivered_at' not in receipt and 'notified_at' not in receipt
        if delivery == 'queued':
            assert receipt['status'] == 'queued'
            assert receipt['receipt_persistence_error']['error_type'] == 'OSError'
            assert invoke() == receipt
        elif surface == 'run':
            assert receipt['error_code'] == 'NOTIFICATION_STORE_UNAVAILABLE'
        else:
            assert 'NOTIFICATION_STORE_UNAVAILABLE' in step.error_message
    assert len(sent) == 1
    assert run.status == 'failed' and run.failed_step_id == 'original-step'


def test_claim_store_failure_stops_before_sending(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import notification_steps

    sent = []
    run = _make_run(tmp_path)
    run.status = 'failed'
    def fail(*args, **kwargs):
        raise OSError('synthetic claim store unavailable')
    monkeypatch.setattr(notification_steps, 'write_text_atomic_beneath', fail)
    receipt = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path,
        notification_sender=lambda **kw: sent.append(kw)), run=run)
    assert receipt['effect_outcome'] == 'not_started' and receipt['stage'] == 'claim_write'
    assert receipt['error_code'] == 'WORKFLOW_NOTIFICATION_RECEIPT_STORE_UNAVAILABLE' and not sent


def test_concurrent_duplicate_failure_is_claimed_before_sender(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    from threading import Event, Lock

    started, release, observed = Event(), Event(), Event()
    calls, guard = [], Lock()
    def sender(**kw):
        with guard:
            calls.append(kw)
        started.set()
        assert release.wait(5)
        return {'status': 'queued'}
    engine = SimpleNamespace(owner_home=tmp_path, notification_sender=sender)
    run = _make_run(tmp_path)
    run.status = 'failed'
    def duplicate():
        try:
            return notify_unattended_run_failure(engine=engine, run=deepcopy(run))
        finally:
            observed.set()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(notify_unattended_run_failure, engine=engine, run=deepcopy(run))
        assert started.wait(5)
        second = pool.submit(duplicate)
        try:
            assert observed.wait(5), '第二事件应在首个发送尚未返回时读取原认领，而不是进入发送端'
        finally:
            release.set()
        results = [first.result(), second.result()]
    assert len(calls) == 1
    assert {x['status'] for x in results} == {'queued', 'dispatching'}
    pending = next(x for x in results if x['status'] == 'dispatching')
    assert pending['effect_outcome'] == 'unknown' and pending['retry_same_request'] is False
    final = notify_unattended_run_failure(engine=engine, run=deepcopy(run))
    assert final['status'] == 'queued' and len(calls) == 1


def test_failure_notice_uses_original_failed_step_and_redacts_reason(tmp_path):
    run = _make_run(tmp_path)
    step = _make_step('original-step', 'script')
    step.status, step.error_code = 'failed', 'COMMAND_FAILED'
    step.error_message = 'expected failure token=synthetic-private-notice'
    run.status, run.failed_step_id, run.steps = 'failed', step.step_id, (step,)
    sent = []
    def sender(**kw):
        sent.append(kw)
        return {'status': 'queued'}
    receipt = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path,
        notification_sender=sender), run=run)
    assert receipt['error_code'] == 'COMMAND_FAILED'
    assert 'expected failure' in receipt['message'] and 'synthetic-private-notice' not in receipt['message']
    assert len(sent) == 1


def _notification_process_probe(root, started, release, completed, timestamp):
    import json
    import os
    from pathlib import Path

    root = Path(root)
    run = _make_run(root)
    run.started_at, run.status = timestamp, 'failed'
    def sender(**kw):
        fd = os.open(root / 'actual-deliveries.jsonl', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps({'pid': os.getpid(), 'key': kw['payload']['dedupe_key']}) + '\n').encode())
        finally:
            os.close(fd)
        started.set()
        assert release.wait(10)
        return {'status': 'queued'}
    try:
        result = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=root,
            notification_sender=sender), run=run)
        (root / f'result-{os.getpid()}.json').write_text(json.dumps(result))
    finally:
        completed.set()


def test_same_notification_is_exclusive_across_processes(tmp_path):
    import json
    import multiprocessing
    import time

    ctx = multiprocessing.get_context('spawn')
    started, release, completed = ctx.Event(), ctx.Event(), ctx.Event()
    timestamp = time.time()
    first = ctx.Process(target=_notification_process_probe,
        args=(str(tmp_path), started, release, completed, timestamp))
    second = ctx.Process(target=_notification_process_probe,
        args=(str(tmp_path), started, release, completed, timestamp))
    first.start()
    try:
        assert started.wait(10)
        second.start()
        assert completed.wait(10), '第二进程应读到持久认领并返回，不进入发送端'
    finally:
        release.set()
        first.join(10)
        if second.pid:
            second.join(10)
        for process in (first, second):
            if process.pid and process.is_alive():
                process.terminate()
                process.join(5)
    assert first.exitcode == second.exitcode == 0
    lines = (tmp_path / 'actual-deliveries.jsonl').read_text().splitlines()
    assert len(lines) == 1
    results = [json.loads(path.read_text()) for path in tmp_path.glob('result-*.json')]
    assert {result['status'] for result in results} == {'dispatching', 'queued'}
    first.close()
    second.close()


def test_crash_window_claim_is_unknown_and_never_replayed(tmp_path):
    from agent_py_agent.agent.workflows.notification_steps import (
        _claim_notification,
        _notification_receipt_path,
    )

    run = _make_run(tmp_path)
    run.status = 'failed'
    key = 'wf-run-fail:' + run.workflow_run_id
    claim_id, prior = _claim_notification(tmp_path, _notification_receipt_path(run, key), key, run)
    assert claim_id and prior is None
    sent = []
    result = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path,
        notification_sender=lambda **kw: sent.append(kw)), run=run)
    assert result['status'] == 'dispatching' and result['effect_outcome'] == 'unknown'
    assert result['retry_same_request'] is False and sent == []


def test_unknown_step_outcome_cannot_be_retried_by_changing_attempt(tmp_path):
    run, step = _make_run(tmp_path), _make_step()
    step.attempt_count = 2
    step.output_snapshot = {'effect_outcome': 'unknown', 'status': 'failed'}
    sent = []
    execute_notification_step(engine=SimpleNamespace(owner_home=tmp_path,
        notification_sender=lambda **kw: sent.append(kw)), run=run, workflow=None, step=step,
        spec={'notification_spec': {'channel': 'im', 'template': 'synthetic'}}, context=None, outputs={})
    assert step.status == 'failed' and step.needs_repair and not sent


def test_old_dated_receipt_is_read_without_rewriting_or_resending(tmp_path):
    import hashlib
    import json
    import time
    from datetime import datetime, timezone

    from agent_py_agent.agent.common.nofollow_fs import read_text_beneath, write_text_atomic_beneath

    run = _make_run(tmp_path)
    run.status, run.started_at, run.ended_at = 'failed', time.time() - 86400, time.time()
    key = 'wf-run-fail:' + run.workflow_run_id
    day = datetime.fromtimestamp(run.ended_at, timezone.utc).strftime('%Y-%m-%d')
    parts = ('data', 'workflow_notifications', day, hashlib.sha256(key.encode()).hexdigest()[:24] + '.json')
    raw = json.dumps({'dedupe_key': key, 'status': 'queued', 'delivery_result': {'status': 'queued'}})
    write_text_atomic_beneath(tmp_path, parts, raw)
    sent = []
    result = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path,
        notification_sender=lambda **kw: sent.append(kw)), run=run)
    assert result['status'] == 'queued' and not sent and read_text_beneath(tmp_path, parts) == raw


def test_corrupt_claim_and_send_then_raise_both_forbid_blind_replay(tmp_path):
    from copy import deepcopy

    from agent_py_agent.agent.common.nofollow_fs import read_text_beneath, write_text_atomic_beneath
    from agent_py_agent.agent.workflows.notification_steps import _notification_receipt_path

    run = _make_run(tmp_path)
    run.status = 'failed'
    key = 'wf-run-fail:' + run.workflow_run_id
    parts = _notification_receipt_path(run, key)
    write_text_atomic_beneath(tmp_path, parts, '{broken')
    sent = []
    engine = SimpleNamespace(owner_home=tmp_path, notification_sender=lambda **kw: sent.append(kw))
    result = notify_unattended_run_failure(engine=engine, run=deepcopy(run))
    assert result['effect_outcome'] == 'unknown' and not sent
    assert read_text_beneath(tmp_path, parts) == '{broken'
    other = _make_run(tmp_path, run_id='send-before-ack-error')
    other.status = 'failed'
    def sender(**kw):
        sent.append(kw)
        raise OSError('synthetic connection lost after enqueue')
    engine.notification_sender = sender
    first = notify_unattended_run_failure(engine=engine, run=deepcopy(other))
    second = notify_unattended_run_failure(engine=engine, run=deepcopy(other))
    assert first['effect_outcome'] == second['effect_outcome'] == 'unknown'
    assert first['retry_same_request'] is False and len(sent) == 1
