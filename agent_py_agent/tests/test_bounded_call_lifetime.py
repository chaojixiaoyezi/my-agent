"""有界调用真实线程/清理退出、启动 ACK 未知和只读 drain 快照合同。"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import bounded_call as bounded
from agent_py_agent.agent.concurrency.interrupt import (
    InterruptHandle,
    register_interrupt_callback,
    register_interruptible,
)


@pytest.fixture
def tracked():
    releases = []
    yield releases
    for release in releases:
        release.set()
    for call in tuple(bounded._CALLS.values()):
        call.interrupt.cancel()
        worker = call.worker
        if worker is not None:
            assert worker._started.wait(2)
            worker.join(2)
            assert not worker.is_alive()
        cleanup = call.interrupt._cleanup_thread
        if cleanup is not None:
            assert cleanup._started.wait(2)
            cleanup.join(2)
            assert not cleanup.is_alive()
        assert not call.retained()
    with bounded._CALL_LOCK:
        bounded._reap_calls_locked()


# LLM: 只创建受事件控制的 Python 宿主线程；最终释放/真实 join，测试不创建进程或外部运行时。
# 函数用途: 观察调用等待与错误，保持生产 worker 和测试驱动线程身份分离。
def _caller(operation, *, key, seconds=1, handle=None, wait_for_exit=True):
    result, done = {}, threading.Event()

    def run():
        try:
            result['value'] = bounded.call_with_deadline(
                operation, deadline=time.monotonic() + seconds, resource_key=key,
                interrupt_handle=handle, wait_for_exit=wait_for_exit,
            )
        except BaseException as error:
            result['error'] = error
        finally:
            done.set()

    thread = threading.Thread(target=run, name=f'lifetime-caller:{key}')
    thread.start()
    return thread, result, done


# LLM: 故障只发生在真实 Thread.start 等原 _started 的 ACK 位置；原生出生已发生但没有假造结束。
# 函数用途: 用真实 bootstrap 门复现 ident 尚未发布时的启动异常，最终由测试释放并 join。
@contextmanager
def _lost_ack(monkeypatch):
    born, release = threading.Event(), threading.Event()
    seen = {}
    original_start, original_bootstrap = threading.Thread.start, threading.Thread._bootstrap_inner

    def bootstrap(worker):
        if worker.name.startswith('bounded-call:'):
            seen['worker'] = worker
            born.set()
            assert release.wait(3)
        return original_bootstrap(worker)

    def start(worker):
        if worker.name.startswith('bounded-call:'):
            original_wait = worker._started.wait
            first = True

            def wait(timeout=None):
                nonlocal first
                if first:
                    first = False
                    assert born.wait(1)
                    raise RuntimeError('controlled-ACK-loss')
                return original_wait(timeout)

            worker._started.wait = wait
        return original_start(worker)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, '_bootstrap_inner', bootstrap)
        patch.setattr(threading.Thread, 'start', start)
        try:
            yield seen, born, release
        finally:
            release.set()
            worker = seen.get('worker')
            if worker is not None:
                assert worker._started.wait(2)
                worker.join(2)
                assert not worker.is_alive()


def test_real_birth_before_ack_failure_keeps_original_call(monkeypatch, tracked):
    operation = Mock()
    with _lost_ack(monkeypatch) as (seen, born, release):
        with pytest.raises(RuntimeError, match='controlled-ACK-loss'):
            bounded.call_with_deadline(operation, deadline=time.monotonic() + 1, resource_key='ack-default')
        assert born.is_set() and seen['worker'].ident is None
        assert 'ack-default' in bounded._CALLS
        call = bounded._CALLS['ack-default']
        assert call.worker is seen['worker'] and call.retained()
        assert bounded.bounded_call_drain_snapshot() == (call,)
        operation.assert_not_called()
        release.set()


def test_mp_unknown_ack_is_bounded_and_retained(monkeypatch, tracked):
    with _lost_ack(monkeypatch) as (seen, born, release):
        before = time.monotonic()
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(lambda: None, deadline=before + .05,
                                       resource_key='ack-mp', wait_for_exit=True)
        assert time.monotonic() - before < .3
        assert born.is_set() and seen['worker'].ident is None
        assert caught.value.reason == 'worker_exit_unconfirmed'
        assert bounded._CALLS['ack-mp'].retained()
        release.set()


def test_attempt_without_birth_is_unknown_not_an_infinite_wait(monkeypatch):
    operation = Mock()
    start = Mock(side_effect=RuntimeError('controlled-no-birth'))
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', start)
        before = time.monotonic()
        try:
            with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
                bounded.call_with_deadline(operation, deadline=before + .04,
                                           resource_key='no-birth', wait_for_exit=True)
            assert time.monotonic() - before < .25
            call = bounded._CALLS['no-birth']
            assert call.retained() and call.worker.ident is None
            assert caught.value.call_id == call.call_id
            assert caught.value.error_code == 'BOUNDED_CALL_EXIT_UNCONFIRMED'
            assert caught.value.automatic_replay is False
            assert 'controlled-no-birth' not in str(caught.value)
            assert 'no-birth' not in str(caught.value)
            operation.assert_not_called()
            start.assert_called_once()
        finally:
            # 唯一故障注入器从未调用真实 start，仅撤销这个已证明无物理线程的合成行，不给产品增加回收旁路。
            bounded._CALLS.pop('no-birth', None)


def test_drain_snapshot_uses_original_table_without_reaping(monkeypatch, tracked):
    with monkeypatch.context() as patch:
        patch.setattr(bounded, '_reap_calls_locked', lambda: None)
        assert bounded.call_with_deadline(lambda: 7, deadline=time.monotonic() + 1, resource_key='snapshot-dead') == 7
    call = bounded._CALLS['snapshot-dead']
    call.worker.join(1)
    before = dict(bounded._CALLS)
    assert bounded.bounded_call_drain_snapshot() == ()
    assert before == bounded._CALLS


def test_result_and_wake_do_not_prove_physical_exit(monkeypatch, tracked):
    tail, release = threading.Event(), threading.Event()
    tracked.append(release)
    original_run = threading.Thread.run

    def run(worker):
        result = original_run(worker)
        if worker.name.startswith('bounded-call:'):
            tail.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(threading.Thread, 'run', run)
    driver, result, done = _caller(lambda: 9, key='physical-tail')
    assert tail.wait(1)
    call = bounded._CALLS['physical-tail']
    assert call.wake.is_set() and call.result == (True, 9)
    assert not done.wait(.03)
    assert call in bounded.bounded_call_drain_snapshot()
    release.set()
    assert done.wait(1)
    driver.join(1)
    assert result == {'value': 9}


def test_mp_deadline_retains_noncooperative_worker_and_safe_typed_error(tracked):
    ready, release = threading.Event(), threading.Event()
    tracked.append(release)

    def operation():
        ready.set()
        release.wait(3)

    before = time.monotonic()
    with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
        bounded.call_with_deadline(operation, deadline=before + .05,
                                   resource_key='private-resource-never-render', wait_for_exit=True)
    assert ready.is_set() and time.monotonic() - before < .3
    call = bounded._CALLS['private-resource-never-render']
    assert caught.value.call_id == call.call_id
    assert caught.value.reason == 'worker_exit_unconfirmed'
    assert caught.value.automatic_replay is False
    assert 'private-resource-never-render' not in str(caught.value)
    assert not isinstance(caught.value, bounded.BoundedCallTimeoutError)
    assert call in bounded.bounded_call_drain_snapshot()


@pytest.mark.parametrize('wait_for_exit', [False, True])
def test_context_and_original_business_exception_preserved_when_exit_is_confirmed(wait_for_exit, tracked):
    marker = contextvars.ContextVar('lifetime-owner', default='missing')
    token = marker.set('synthetic-owner')
    error = ValueError('original-business-error')

    def operation():
        assert marker.get() == 'synthetic-owner'
        raise error

    try:
        with pytest.raises(ValueError) as caught:
            bounded.call_with_deadline(operation, deadline=time.monotonic() + 1,
                                       resource_key='exception-context', wait_for_exit=wait_for_exit)
        assert caught.value is error
    finally:
        marker.reset(token)


@pytest.mark.parametrize('field', ['worker', 'interrupt'])
def test_replacing_retained_owner_reference_cannot_wash_responsibility(field, tracked):
    release = threading.Event()
    tracked.append(release)
    with pytest.raises(bounded.BoundedCallStillRunningError):
        bounded.call_with_deadline(lambda: release.wait(3), deadline=time.monotonic() + .04,
                                   resource_key=f'replaced-{field}')
    call = bounded._CALLS[f'replaced-{field}']
    original = getattr(call, field)
    release.set()
    call.worker.join(1)
    try:
        setattr(call, field, threading.Thread() if field == 'worker' else InterruptHandle())
        assert call.retained()
        assert bounded.bounded_call_drain_snapshot() == (call,)
    finally:
        setattr(call, field, original)


@pytest.mark.parametrize('value', [None, 0, 1, 'false'])
def test_wait_for_exit_accepts_only_actual_bool_before_claim(value):
    operation = Mock()
    with pytest.raises(ValueError):
        bounded.call_with_deadline(operation, deadline=time.monotonic() + 1,
                                   resource_key='bad-wait-option', wait_for_exit=value)
    assert 'bad-wait-option' not in bounded._CALLS
    operation.assert_not_called()


def test_cleanup_done_event_is_not_physical_exit(monkeypatch):
    handle = InterruptHandle()
    tail, release = threading.Event(), threading.Event()
    original_run = threading.Thread.run

    def run(worker):
        result = original_run(worker)
        if worker.name == 'interrupt-handle-cleanup':
            tail.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(threading.Thread, 'run', run)
    try:
        with register_interruptible('cleanup-tail', handle=handle), register_interrupt_callback(lambda: None):
            handle.cancel()
        assert tail.wait(1) and handle._cleanup_done.is_set()
        assert handle.cleanup_pending
        before = time.monotonic()
        assert not handle.wait_cleanup_until(before + .04)
        assert time.monotonic() - before < .25
    finally:
        release.set()
        if handle._cleanup_thread is not None:
            handle._cleanup_thread.join(1)
    assert handle.wait_cleanup_until(time.monotonic() + 1)
    assert not handle.cleanup_pending


def test_cleanup_start_failure_stays_unknown_and_is_not_retried(monkeypatch):
    handle = InterruptHandle()
    callback = Mock()
    start = Mock(side_effect=RuntimeError('controlled-cleanup-no-birth'))
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, 'start', start)
        with register_interruptible('cleanup-start-failure', handle=handle), register_interrupt_callback(callback):
            handle.cancel()
            handle.cancel()
        before = time.monotonic()
        assert not handle.wait_cleanup_until(before + .04)
        assert time.monotonic() - before < .25
    assert handle.cleanup_pending
    start.assert_called_once()
    callback.assert_not_called()


def test_cleanup_reference_replacement_cannot_fake_done():
    handle = InterruptHandle()
    with register_interruptible('cleanup-replace', handle=handle), register_interrupt_callback(lambda: None):
        handle.cancel()
    original = handle._cleanup_thread
    original.join(1)
    try:
        handle._cleanup_thread = threading.Thread()
        assert handle.cleanup_pending
        assert not handle.wait_cleanup_until(time.monotonic() + .04)
    finally:
        handle._cleanup_thread = original
    assert not handle.cleanup_pending


def test_mp_cancel_preserves_interrupt_only_after_worker_and_cleanup_exit(tracked):
    handle = InterruptHandle()
    ready, worker_release, cleanup_started, cleanup_release = (threading.Event() for _ in range(4))
    tracked.extend([worker_release, cleanup_release])

    def cleanup():
        cleanup_started.set()
        worker_release.set()
        cleanup_release.wait(3)

    def operation():
        with register_interrupt_callback(cleanup):
            ready.set()
            worker_release.wait(3)

    driver, result, done = _caller(operation, key='mp-cancel-cleanup', handle=handle)
    assert ready.wait(1)
    handle.cancel()
    assert cleanup_started.wait(1)
    call = bounded._CALLS['mp-cancel-cleanup']
    call.worker.join(1)
    assert handle.cleanup_pending and not done.wait(.03)
    cleanup_release.set()
    assert done.wait(1)
    driver.join(1)
    assert isinstance(result['error'], InterruptedError)
    assert not call.retained()


def test_mp_cleanup_constructor_failure_returns_unknown_without_retry(monkeypatch, tracked):
    handle = InterruptHandle()
    ready, release = threading.Event(), threading.Event()
    tracked.append(release)
    callback = Mock()

    def operation():
        with register_interrupt_callback(callback):
            ready.set()
            release.wait(3)

    driver, result, done = _caller(operation, key='mp-cleanup-no-thread', handle=handle, seconds=.1)
    assert ready.wait(1)
    constructor = Mock(side_effect=RuntimeError('controlled-cleanup-construction-failure'))
    with monkeypatch.context() as patch:
        patch.setattr(threading, 'Thread', constructor)
        handle.cancel()
        release.set()
        assert done.wait(1)
    driver.join(1)
    error = result['error']
    assert isinstance(error, bounded.BoundedCallExitUnconfirmedError)
    assert error.reason == 'cleanup_exit_unconfirmed'
    constructor.assert_called_once()
    callback.assert_not_called()
    call = bounded._CALLS['mp-cleanup-no-thread']
    assert call.retained() and handle.cleanup_pending
    # 故障构造器确未创建任何清理线程，只清本精确合成记录；生产不得以无句柄认定清理完成。
    bounded._CALLS.pop('mp-cleanup-no-thread')


@pytest.mark.parametrize('field', ['worker', 'interrupt'])
def test_mp_rechecks_original_owner_reference_after_join(field, monkeypatch, tracked):
    from agent_py_agent.agent.concurrency.interrupt import _ThreadExitObservation

    original_wait = _ThreadExitObservation.wait_until
    seen = {}

    def wait(observation, deadline):
        result = original_wait(observation, deadline)
        if observation.thread.name.startswith('bounded-call:'):
            call = bounded._CALLS['swap-after-join']
            seen['call'], seen['original'] = call, getattr(call, field)
            setattr(call, field, threading.Thread() if field == 'worker' else InterruptHandle())
        return result

    monkeypatch.setattr(_ThreadExitObservation, 'wait_until', wait)
    try:
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(lambda: 1, deadline=time.monotonic() + 1,
                                       resource_key='swap-after-join', wait_for_exit=True)
        assert caught.value.reason == f'{field}_identity_changed'
        assert seen['call'].retained()
    finally:
        if 'call' in seen:
            setattr(seen['call'], field, seen['original'])


def test_mp_cleanup_uses_original_remaining_budget_and_stays_retained(tracked):
    handle = InterruptHandle()
    ready, worker_release, cleanup_started, cleanup_release = (threading.Event() for _ in range(4))
    tracked.extend([worker_release, cleanup_release])

    def cleanup():
        cleanup_started.set()
        worker_release.set()
        cleanup_release.wait(3)

    def operation():
        with register_interrupt_callback(cleanup):
            ready.set()
            worker_release.wait(3)

    before = time.monotonic()
    driver, result, done = _caller(operation, key='cleanup-deadline', handle=handle, seconds=.08)
    assert ready.wait(1)
    handle.cancel()
    assert cleanup_started.wait(1)
    assert done.wait(.3)
    assert time.monotonic() - before < .3
    driver.join(1)
    assert isinstance(result['error'], bounded.BoundedCallExitUnconfirmedError)
    assert result['error'].reason == 'cleanup_exit_unconfirmed'
    assert bounded._CALLS['cleanup-deadline'].retained()


def test_cleanup_wait_empty_handle_has_no_side_effects(monkeypatch):
    handle = InterruptHandle()
    start = Mock(side_effect=AssertionError('等待不能创建清理线程'))
    monkeypatch.setattr(threading.Thread, 'start', start)
    assert handle.wait_cleanup_until(time.monotonic() + 1)
    assert not handle.cleanup_pending and not handle.cancelled
    start.assert_not_called()


@pytest.mark.parametrize('deadline', [None, True, float('nan'), float('inf'), 10 ** 1000])
def test_cleanup_wait_rejects_bad_deadline_without_start(deadline):
    handle = InterruptHandle()
    with pytest.raises(ValueError):
        handle.wait_cleanup_until(deadline)
    assert not handle.cancelled and handle._cleanup_thread is None


def test_original_started_and_tstate_reference_replacement_keeps_reservation(tracked):
    release = threading.Event()
    tracked.append(release)
    with pytest.raises(bounded.BoundedCallStillRunningError):
        bounded.call_with_deadline(lambda: release.wait(3), deadline=time.monotonic() + .03,
                                   resource_key='birth-reference')
    call = bounded._CALLS['birth-reference']
    original_started = call.worker._started
    original_lock = getattr(call.worker, '_tstate_lock', None)
    try:
        call.worker._started = threading.Event()
        assert call.retained()
        assert not call.worker_observation.wait_until(time.monotonic() + .03)
        call.worker._started = original_started
        # 当前 CPython 3.12 的真实退出锁替换为未锁新对象，也不能把原活线程洗成已退出。
        if original_lock is not None:
            call.worker._tstate_lock = threading.Lock()
            assert call.retained()
    finally:
        call.worker._started = original_started
        if original_lock is not None:
            call.worker._tstate_lock = original_lock
