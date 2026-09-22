"""通用有界 worker 的期限、准确取消、保留和 ContextVar 传播。"""
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
    interrupt_by_name,
    is_interrupted,
    register_interrupt_callback,
    register_interruptible,
)


@pytest.fixture
def releases():
    events = []
    yield events
    for event in events:
        event.set()
    for call in tuple(bounded._CALLS.values()):
        call.interrupt.cancel()
        if call.worker is not None:
            call.worker.join(3)
        cleanup = call.interrupt._cleanup_thread
        if cleanup is not None and cleanup.ident is not None:
            cleanup.join(3)
        assert not call.retained()
    with bounded._CALL_LOCK:
        bounded._reap_calls_locked()


# LLM: 测试 caller 的命名身份独立于被测 worker；所有等待可由事件释放，不使用收费网络。
# 函数用途: 启动可准确取消的宿主调用，保留异常和完成事件供竞态断言。
def _caller(operation, *, key, seconds=2, handle=None, optional=False, outer_cleanup=None):
    result = {}
    done = threading.Event()
    name = f"test-caller:{key}"

    def run():
        try:
            with register_interruptible(name), register_interrupt_callback(outer_cleanup or (lambda: None)):
                result["value"] = bounded.call_with_deadline(
                    operation, deadline=time.monotonic() + seconds, resource_key=key,
                    interrupt_handle=handle, optional=optional,
                )
        except BaseException as exc:
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result, done, name


def test_context_and_original_result_or_exception_are_preserved(releases):
    marker = contextvars.ContextVar("bounded-test", default="missing")
    token = marker.set("caller-context")
    try:
        assert bounded.call_with_deadline(marker.get, deadline=time.monotonic() + 2, resource_key="context") == "caller-context"
        error = ValueError("original")

        def fail():
            raise error

        with pytest.raises(ValueError) as caught:
            bounded.call_with_deadline(fail, deadline=time.monotonic() + 2, resource_key="exception")
        assert caught.value is error
    finally:
        marker.reset(token)


def test_deadline_returns_without_join_and_retains_noncooperative_worker(releases, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    releases.append(release)

    def operation():
        started.set()
        release.wait(3)
        return "late-result"

    monkeypatch.setattr(threading.Thread, "join", Mock(side_effect=AssertionError("caller 不得 join")))
    started_at = time.monotonic()
    with pytest.raises(bounded.BoundedCallStillRunningError):
        bounded.call_with_deadline(operation, deadline=started_at + 0.05, resource_key="stuck")
    assert time.monotonic() - started_at < 0.25
    assert started.is_set()
    assert bounded._CALLS["stuck"].worker.is_alive()
    with pytest.raises(bounded.BoundedCallBusyError) as caught:
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="stuck")
    assert caught.value.reason == "resource_busy"
    monkeypatch.undo()


def test_cancellation_before_worker_registration_is_not_lost(releases, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    releases.append(release)
    original = bounded.register_interruptible
    operation = Mock()
    handle = InterruptHandle()

    @contextmanager
    def delayed_registration(*args, **kwargs):
        entered.set()
        release.wait(3)
        with original(*args, **kwargs):
            yield

    monkeypatch.setattr(bounded, "register_interruptible", delayed_registration)
    thread, outcome, done, name = _caller(operation, key="pre-register", handle=handle)
    assert entered.wait(1)
    assert interrupt_by_name(name)
    assert done.wait(0.3)
    assert isinstance(outcome["error"], InterruptedError)
    assert handle.cancelled
    assert bounded._CALLS["pre-register"].worker.is_alive()
    release.set()
    bounded._CALLS["pre-register"].worker.join(1)
    thread.join(1)
    operation.assert_not_called()


def test_user_cancel_wakes_before_slow_parent_cleanup(releases):
    release, started, cleanup_release = threading.Event(), threading.Event(), threading.Event()
    releases.extend([release, cleanup_release])

    def operation():
        started.set()
        release.wait(3)

    thread, outcome, done, name = _caller(operation, key="parent-slow-cleanup", outer_cleanup=cleanup_release.wait)
    assert started.wait(1)
    assert interrupt_by_name(name)
    assert done.wait(0.3)
    assert isinstance(outcome["error"], InterruptedError)
    assert not is_interrupted()
    thread.join(1)


def test_explicit_handle_cancel_only_stops_its_own_call(releases):
    release, ready_a, ready_b = threading.Event(), threading.Event(), threading.Event()
    releases.append(release)
    handle = InterruptHandle()

    def block(ready):
        ready.set()
        release.wait(3)
        return is_interrupted()

    a, outcome_a, done_a, _ = _caller(lambda: block(ready_a), key="sibling-a", handle=handle)
    b, outcome_b, done_b, _ = _caller(lambda: block(ready_b), key="sibling-b")
    assert ready_a.wait(1) and ready_b.wait(1)
    handle.cancel()
    assert done_a.wait(0.3)
    assert isinstance(outcome_a["error"], InterruptedError)
    assert not done_b.is_set() and not is_interrupted()
    release.set()
    assert done_b.wait(1)
    assert outcome_b["value"] is False
    a.join(1)
    b.join(1)


def test_cleanup_keeps_reservation_after_worker_exits_and_cancel_is_deduplicated(releases):
    worker_exit, cleanup_started, cleanup_release = threading.Event(), threading.Event(), threading.Event()
    releases.extend([worker_exit, cleanup_release])
    handle = InterruptHandle()
    calls = []

    def cleanup():
        calls.append(1)
        cleanup_started.set()
        worker_exit.set()
        cleanup_release.wait(3)

    def operation():
        with register_interrupt_callback(cleanup):
            worker_exit.wait(3)

    with pytest.raises(bounded.BoundedCallStillRunningError):
        bounded.call_with_deadline(operation, deadline=time.monotonic() + 0.05, resource_key="cleanup", interrupt_handle=handle)
    assert cleanup_started.wait(1)
    call = bounded._CALLS["cleanup"]
    call.worker.join(1)
    assert not call.worker.is_alive() and handle.cleanup_pending
    cleanup_thread = handle._cleanup_thread
    for _ in range(20):
        handle.cancel()
    assert handle._cleanup_thread is cleanup_thread and calls == [1]
    with pytest.raises(bounded.BoundedCallBusyError):
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="cleanup")
    cleanup_release.set()
    cleanup_thread.join(1)
    assert bounded.call_with_deadline(lambda: "new", deadline=time.monotonic() + 2, resource_key="cleanup") == "new"


def test_process_capacity_reserves_one_ordinary_call(releases, monkeypatch):
    monkeypatch.setattr(bounded, "_MAX_RETAINED_CALLS", 3)
    release = threading.Event()
    releases.append(release)
    for key in ("optional-1", "optional-2"):
        with pytest.raises(bounded.BoundedCallStillRunningError):
            bounded.call_with_deadline(lambda: release.wait(3), deadline=time.monotonic() + 0.03, resource_key=key, optional=True)
    with pytest.raises(bounded.BoundedCallBusyError) as caught:
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="optional-3", optional=True)
    assert caught.value.reason == "capacity_exhausted"
    with pytest.raises(bounded.BoundedCallStillRunningError):
        bounded.call_with_deadline(lambda: release.wait(3), deadline=time.monotonic() + 0.03, resource_key="ordinary")
    with pytest.raises(bounded.BoundedCallBusyError):
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="ordinary-over-cap")
    assert len(bounded._CALLS) == 3


@pytest.mark.parametrize("stage", ["context", "construction", "start"])
def test_preparation_failure_releases_unstarted_call(stage, releases, monkeypatch):
    error = RuntimeError("thread setup failed")
    if stage == "context":
        monkeypatch.setattr(bounded.contextvars, "copy_context", Mock(side_effect=error))
    elif stage == "construction":
        monkeypatch.setattr(bounded.threading, "Thread", Mock(side_effect=error))
    else:
        monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=error))
    with pytest.raises(RuntimeError) as caught:
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="setup-failure")
    assert caught.value is error
    assert "setup-failure" not in bounded._CALLS


def test_handle_cannot_be_reused_after_completion_or_cancellation(releases):
    handle = InterruptHandle()
    bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="handle-1", interrupt_handle=handle)
    with pytest.raises(ValueError):
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="handle-2", interrupt_handle=handle)
    cancelled = InterruptHandle()
    cancelled.cancel()
    operation = Mock()
    with pytest.raises(InterruptedError):
        bounded.call_with_deadline(operation, deadline=time.monotonic() + 2, resource_key="pre-cancel", interrupt_handle=cancelled)
    operation.assert_not_called()


def test_running_handle_cannot_be_bound_to_another_resource(releases):
    handle = InterruptHandle()
    ready, release = threading.Event(), threading.Event()
    releases.append(release)

    def operation():
        ready.set()
        release.wait(3)
        return "original"

    thread, outcome, done, _ = _caller(operation, key="original-handle", handle=handle)
    assert ready.wait(1)
    with pytest.raises(ValueError):
        bounded.call_with_deadline(lambda: None, deadline=time.monotonic() + 2, resource_key="reused-handle", interrupt_handle=handle)
    assert not handle.cancelled
    release.set()
    assert done.wait(1)
    assert outcome["value"] == "original"
    thread.join(1)


def test_large_finite_deadline_does_not_overflow_wait(releases):
    ready = threading.Event()
    handle = InterruptHandle()
    release = threading.Event()
    releases.append(release)

    def operation():
        ready.set()
        release.wait(3)

    thread, outcome, done, _ = _caller(operation, key="large-deadline", seconds=threading.TIMEOUT_MAX * 2, handle=handle)
    assert ready.wait(1)
    handle.cancel()
    assert done.wait(0.3)
    assert isinstance(outcome["error"], InterruptedError)
    thread.join(1)


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7)])
def test_caller_base_exception_cancels_only_worker_and_retains_original(error, releases, monkeypatch):
    ready, release = threading.Event(), threading.Event()
    releases.append(release)
    handle = InterruptHandle()

    def operation():
        ready.set()
        release.wait(3)

    def fail_wait(*_args):
        assert ready.wait(1)
        raise error

    monkeypatch.setattr(bounded, "_await_call", fail_wait)
    with pytest.raises(type(error)) as caught:
        bounded.call_with_deadline(operation, deadline=time.monotonic() + 2, resource_key="base-exception", interrupt_handle=handle)
    assert caught.value is error
    assert handle.cancelled and not is_interrupted()
    assert bounded._CALLS["base-exception"].retained()


def test_user_cancel_takes_priority_over_simultaneous_deadline(releases, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bounded.time, "monotonic", lambda: clock[0])
    ready, release = threading.Event(), threading.Event()
    releases.append(release)

    def operation():
        ready.set()
        release.wait(3)
        return "late"

    thread, outcome, done, name = _caller(operation, key="cancel-at-deadline")
    assert ready.wait(1)
    clock[0] = 103.0
    assert interrupt_by_name(name)
    assert done.wait(0.3)
    assert isinstance(outcome["error"], InterruptedError)
    thread.join(1)
