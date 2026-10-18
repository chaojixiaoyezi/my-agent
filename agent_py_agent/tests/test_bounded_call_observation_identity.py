"""原 worker/cleanup 观察对象丢失或换写时不能确认物理退出。"""
from __future__ import annotations

import copy
import threading
import time
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import bounded_call as bounded
from agent_py_agent.agent.concurrency.interrupt import (
    InterruptHandle,
    register_interrupt_callback,
    register_interruptible,
)


# LLM: None、复制和新建同 Thread 都只改可见观察字段，不触原物理线程；不能用替身死亡伪造原责任结束。
# 函数用途: 生成三种真实身份漂移输入，便于 worker/cleanup 共用负例。
def _replacement(observation, variant):
    if variant == 'missing':
        return None
    if variant == 'copy':
        return copy.copy(observation)
    return type(observation)(observation.thread)


@pytest.mark.parametrize('variant', ['missing', 'copy', 'new-same-thread'])
def test_worker_observation_replacement_is_unknown_after_result(variant, monkeypatch):
    tail, release = threading.Event(), threading.Event()
    original_run, original_await = threading.Thread.run, bounded._await_call
    seen = {}

    def run(worker):
        value = original_run(worker)
        if worker.name.startswith('bounded-call:'):
            tail.set()
            assert release.wait(3)
        return value

    def drift(call, deadline):
        result = original_await(call, deadline)
        assert tail.wait(1)
        seen['call'], seen['worker'], seen['observation'] = call, call.worker, call.worker_observation
        call.worker_observation = _replacement(call.worker_observation, variant)
        return result

    monkeypatch.setattr(threading.Thread, 'run', run)
    monkeypatch.setattr(bounded, '_await_call', drift)
    try:
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(lambda: 7, deadline=time.monotonic() + .08,
                                       resource_key='observation-drift', wait_for_exit=True)
        assert caught.value.reason == 'worker_identity_changed'
        assert seen['worker'].is_alive() and seen['call'].retained()
        assert bounded.bounded_call_drain_snapshot() == (seen['call'],)
    finally:
        if 'call' in seen:
            seen['call'].worker_observation = seen['observation']
            release.set()
            seen['worker'].join(1)
            assert not seen['call'].retained()
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


@pytest.mark.parametrize('variant', ['missing', 'copy', 'new-same-thread'])
def test_cleanup_observation_replacement_keeps_original_thread(variant, monkeypatch):
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
    original = None
    try:
        with register_interruptible('cleanup-observer', handle=handle), register_interrupt_callback(lambda: None):
            handle.cancel()
        assert tail.wait(1) and handle._cleanup_done.is_set()
        original = handle._cleanup_observation
        replacement = _replacement(original, variant)
        forbidden = Mock(side_effect=AssertionError('替换观察对象不能成为等待权威'))
        if replacement is not None:
            replacement.wait_until = forbidden
        handle._cleanup_observation = replacement
        assert handle.cleanup_pending
        assert not handle.wait_cleanup_until(time.monotonic() + .03)
        forbidden.assert_not_called()
    finally:
        if original is not None:
            handle._cleanup_observation = original
        release.set()
        if handle._cleanup_thread is not None:
            handle._cleanup_thread.join(1)
    assert not handle.cleanup_pending


def test_observer_and_call_worker_fields_cannot_drift_together(monkeypatch):
    # 对照线程真实启动并退出；不能用它的已退出字段清掉另一个仍活的原线程。
    decoy = threading.Thread(target=lambda: None, name='observation-decoy')
    decoy.start()
    decoy.join(1)
    tail, release = threading.Event(), threading.Event()
    original_run, original_await = threading.Thread.run, bounded._await_call
    seen = {}

    def run(worker):
        result = original_run(worker)
        if worker.name.startswith('bounded-call:'):
            tail.set()
            assert release.wait(3)
        return result

    def drift(call, deadline):
        result = original_await(call, deadline)
        assert tail.wait(1)
        observation = call.worker_observation
        seen.update(call=call, worker=call.worker, observation=observation,
                    fields={name: getattr(observation, name) for name in ('thread', 'started', '_tstate_lock')})
        call.worker = decoy
        observation.thread, observation.started = decoy, decoy._started
        observation._tstate_lock = None
        return result

    monkeypatch.setattr(threading.Thread, 'run', run)
    monkeypatch.setattr(bounded, '_await_call', drift)
    try:
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(lambda: 7, deadline=time.monotonic() + .08,
                                       resource_key='observer-fields', wait_for_exit=True)
        assert caught.value.reason == 'worker_identity_changed'
        assert seen['worker'].is_alive() and seen['call'].retained()
    finally:
        if 'call' in seen:
            seen['call'].worker = seen['worker']
            for name, value in seen['fields'].items():
                setattr(seen['observation'], name, value)
            release.set()
            seen['worker'].join(1)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


def test_started_view_and_actual_thread_notification_cannot_drift_together(monkeypatch):
    tail, release = threading.Event(), threading.Event()
    original_run, original_await = threading.Thread.run, bounded._await_call
    seen = {}

    def run(worker):
        result = original_run(worker)
        if worker.name.startswith('bounded-call:'):
            tail.set()
            assert release.wait(3)
        return result

    def drift(call, deadline):
        result = original_await(call, deadline)
        assert tail.wait(1)
        observation = call.worker_observation
        seen.update(call=call, observation=observation, started=call.worker._started)
        swapped = threading.Event()
        swapped.set()
        call.worker._started = observation.started = swapped
        return result

    monkeypatch.setattr(threading.Thread, 'run', run)
    monkeypatch.setattr(bounded, '_await_call', drift)
    try:
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(lambda: 7, deadline=time.monotonic() + .05,
                                       resource_key='notification-drift', wait_for_exit=True)
        assert caught.value.reason == 'worker_identity_changed'
        assert seen['call'].worker.is_alive() and seen['call'].retained()
    finally:
        if 'call' in seen:
            seen['call'].worker._started = seen['observation'].started = seen['started']
            release.set()
            seen['call'].worker.join(1)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


def test_start_attempt_fact_cannot_be_reset(monkeypatch):
    release, ready = threading.Event(), threading.Event()
    handle = InterruptHandle()
    try:
        with pytest.raises(bounded.BoundedCallStillRunningError):
            bounded.call_with_deadline(lambda: (ready.set(), release.wait(3)), deadline=time.monotonic() + .04,
                                       resource_key='start-fact', interrupt_handle=handle)
        call = bounded._CALLS['start-fact']
        observation = call.worker_observation
        assert ready.is_set() and observation.attempted
        changed = False
        try:
            try:
                observation.attempted = False
                changed = True
            except AttributeError:
                pass
            assert not changed
            assert call.retained()
        finally:
            if changed:
                observation.attempted = True
    finally:
        release.set()
        if 'start-fact' in bounded._CALLS:
            bounded._CALLS['start-fact'].worker.join(1)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


@pytest.mark.parametrize('wait_for_exit', [False, True])
def test_observer_construction_failure_is_known_never_started(wait_for_exit, monkeypatch):
    error = RuntimeError('controlled-before-observation-binding')
    start = Mock(side_effect=AssertionError('不能启动未完成准备的线程'))
    monkeypatch.setattr(bounded, '_ThreadExitObservation', Mock(side_effect=error))
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        with pytest.raises(RuntimeError) as caught:
            bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 1,
                                       resource_key='observer-construction', wait_for_exit=wait_for_exit)
        assert caught.value is error
        assert 'observer-construction' not in bounded._CALLS
        start.assert_not_called()
    finally:
        # 构造器明确在 start 前失败，仅清此合成行，避免旧产品的保守保留污染其它原红。
        bounded._CALLS.pop('observer-construction', None)


def test_bound_original_anchors_cannot_be_replaced():
    release, ready = threading.Event(), threading.Event()
    try:
        with pytest.raises(bounded.BoundedCallStillRunningError):
            bounded.call_with_deadline(lambda: (ready.set(), release.wait(3)), deadline=time.monotonic() + .04,
                                       resource_key='bound-anchors')
        call = bounded._CALLS['bound-anchors']
        observation = call.worker_observation
        assert ready.is_set()
        for target, name, value in (
            (call, '_worker_observation_origin', None), (call, 'interrupt_origin', InterruptHandle()),
            (observation, '_identity', copy.copy(observation._identity)),
            (observation, '_start_attempted', False),
        ):
            with pytest.raises(AttributeError):
                setattr(target, name, value)
        assert call.retained()
    finally:
        release.set()
        if 'bound-anchors' in bounded._CALLS:
            bounded._CALLS['bound-anchors'].worker.join(1)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()
