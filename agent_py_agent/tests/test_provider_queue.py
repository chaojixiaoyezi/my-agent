from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from itertools import product
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import request_scope as scope
from agent_py_agent.agent.backends.errors import ProviderQueueTimeoutError
from agent_py_agent.agent.concurrency.interrupt import register_interruptible, set_interrupt


def request(limit=2, endpoint="http://queue.invalid/v1"):
    return SimpleNamespace(api_base=endpoint, provider_max_concurrent_requests=limit)


def wait_until(check):
    deadline = time.monotonic() + 2
    while not check() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert check()


def test_ten_requests_never_exceed_two_and_return_all_slots():
    active = peak = 0
    lock = threading.Lock()

    def run(i):
        nonlocal active, peak
        with scope.provider_owner_scope(str(i)):
            lease = scope.acquire_provider_request(request())
        try:
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.015)
            with lock:
                active -= 1
        finally:
            lease.release()
        return i

    with ThreadPoolExecutor(max_workers=10) as pool:
        assert sorted(pool.map(run, range(10))) == list(range(10))
    assert peak == 2
    assert scope.provider_endpoint_counts(request()) == (0, 0)


def test_owner_round_robin_and_fifo():
    req = request(1)
    held = scope.acquire_provider_request(req)
    order = []

    def run(owner, i):
        with scope.provider_owner_scope(owner):
            lease = scope.acquire_provider_request(req)
        try:
            order.append((owner, i))
        finally:
            lease.release()

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = []
        for i in range(6):
            jobs.append(pool.submit(run, "A", i))
            wait_until(lambda: scope.provider_endpoint_counts(req)[1] == i + 1)
        for i in range(2):
            jobs.append(pool.submit(run, "B", i))
            wait_until(lambda: scope.provider_endpoint_counts(req)[1] == 7 + i)
        held.release()
        for job in jobs:
            job.result(timeout=2)
    assert order.index(("B", 0)) <= 1
    assert [i for owner, i in order if owner == "A"] == list(range(6))
    assert [i for owner, i in order if owner == "B"] == [0, 1]
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_wait_is_interruptible_without_stealing_held_slot():
    req = request(1)
    held = scope.acquire_provider_request(req)
    errors = []

    def run():
        with register_interruptible("queue-stop"):
            try:
                scope.acquire_provider_request(req)
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        wait_until(lambda: scope.provider_endpoint_counts(req) == (1, 1))
        set_interrupt(True, thread.ident)
        thread.join(timeout=0.5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert scope.provider_endpoint_counts(req) == (1, 0)
    finally:
        held.release()
        thread.join(timeout=2)


def test_stop_racing_with_grant_returns_only_its_own_slot(monkeypatch):
    req = request(1)
    held = scope.acquire_provider_request(req)
    original = scope._ticket_ready

    def stop_after_grant(ticket, started):
        if ticket.granted:
            set_interrupt(True)
        return original(ticket, started)

    def run():
        with register_interruptible("grant-race"), pytest.raises(InterruptedError):
            scope.acquire_provider_request(req)

    monkeypatch.setattr(scope, "_ticket_ready", stop_after_grant)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(run)
        wait_until(lambda: scope.provider_endpoint_counts(req) == (1, 1))
        held.release()
        job.result(timeout=2)
    held.release()  # 重复归还不多减。
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_queue_timeout_is_transient_and_cleans_waiter(monkeypatch):
    from agent_py_agent.agent.backends.errors import is_provider_transient_error

    monkeypatch.setattr(scope, "PROVIDER_QUEUE_TIMEOUT_SECONDS", 0.025)
    req = request(1)
    held = scope.acquire_provider_request(req)
    try:
        with pytest.raises(ProviderQueueTimeoutError) as raised:
            scope.acquire_provider_request(req)
        assert raised.value.error_code == "PROVIDER_QUEUE_TIMEOUT"
        assert is_provider_transient_error(raised.value)
        assert scope.provider_endpoint_counts(req) == (1, 0)
    finally:
        held.release()


def test_zero_bypasses_queue_and_lock(monkeypatch):
    class ForbiddenLock:
        def __enter__(self):
            pytest.fail("disabled limiter acquired a lock")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(scope, "_LOCK", ForbiddenLock())
    assert scope.acquire_provider_request(request(0)) is None


def test_endpoint_identity_is_shared_without_credentials_and_isolated():
    first = request(1, "https://user:secret@queue.invalid/v1")
    same = request(1, "https://queue.invalid:443/other")
    other = request(1, "https://other.invalid/v1")
    held = scope.acquire_provider_request(first)
    independent = scope.acquire_provider_request(other)
    try:
        assert scope.provider_endpoint_counts(same) == (1, 0)
        assert scope.provider_endpoint_counts(other) == (1, 0)
        assert all("secret" not in repr(key) for key in scope._INFLIGHT)
    finally:
        held.release()
        independent.release()
    assert not scope._INFLIGHT


def test_queue_notice_and_clear_are_structured(monkeypatch):
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_NOTICE_SECONDS", 0.01)
    req = request(1)
    held = scope.acquire_provider_request(req)
    events = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(scope.acquire_provider_request, req, events.append)
        try:
            wait_until(lambda: events)
            assert events[0]["phase"] == "waiting"
            assert events[0]["admission_wait_reason"] == "provider_concurrency"
            assert "owner" not in events[0] and "endpoint" not in events[0]
        finally:
            held.release()
        job.result(timeout=2).release()
    assert events[-1]["phase"] == "admitted"


def test_queue_length_is_bounded(monkeypatch):
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_MAX_WAITER_COUNT", 0)
    req = request(1)
    held = scope.acquire_provider_request(req)
    try:
        with pytest.raises(ProviderQueueTimeoutError) as raised:
            scope.acquire_provider_request(req)
        assert raised.value.error_code == "PROVIDER_QUEUE_TIMEOUT"
        assert scope.provider_endpoint_counts(req) == (1, 0)
    finally:
        held.release()


def test_endpoint_resources_are_bounded_and_removed_after_release(monkeypatch):
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_MAX_ENDPOINT_COUNT", 1)
    req = request(1)
    held = scope.acquire_provider_request(req)
    try:
        with pytest.raises(ProviderQueueTimeoutError) as raised:
            scope.acquire_provider_request(request(1, "http://second.invalid"))
        assert raised.value.error_code == "PROVIDER_QUEUE_TIMEOUT"
        assert len(scope._INFLIGHT) == 1
    finally:
        held.release()
    assert not scope._INFLIGHT


def test_three_owner_rotation_never_starves_c():
    req = request(1)
    held = scope.acquire_provider_request(req)
    order = []

    def run(owner, number):
        with scope.provider_owner_scope(owner):
            lease = scope.acquire_provider_request(req)
        try:
            order.append((owner, number))
        finally:
            lease.release()

    with ThreadPoolExecutor(max_workers=9) as pool:
        jobs = []
        for owner, number in product(("A", "B", "C"), range(3)):
            jobs.append(pool.submit(run, owner, number))
            wait_until(lambda: scope.provider_endpoint_counts(req)[1] == len(jobs))
        held.release()
        for job in jobs:
            job.result(timeout=2)
    assert order == [(owner, number) for number in range(3) for owner in ("A", "B", "C")]
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_repeated_cancel_reclaims_capacity_and_empty_buckets(monkeypatch):
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_MAX_WAITER_COUNT", 2)
    req = request(1)
    held = scope.acquire_provider_request(req)
    threads = []

    def run(owner):
        with register_interruptible(f"capacity-{owner}"), scope.provider_owner_scope(owner), pytest.raises(InterruptedError):
            threads.append(threading.get_ident())
            scope.acquire_provider_request(req)

    with ExitStack() as stack:
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=2))
        stack.callback(held.release)
        for _ in range(3):
            threads.clear()
            jobs = [pool.submit(run, owner) for owner in ("A", "B")]
            wait_until(lambda: scope.provider_endpoint_counts(req) == (1, 2))
            _assert_queue_full(req)
            _stop_waiting_threads(threads, jobs)
            assert scope.provider_endpoint_counts(req) == (1, 0)
            assert not scope._INFLIGHT[scope._endpoint_key(req)].owners
    assert not scope._INFLIGHT
    scope.acquire_provider_request(req).release()
    assert not scope._INFLIGHT


def _assert_queue_full(req):
    with pytest.raises(ProviderQueueTimeoutError) as raised:
        scope.acquire_provider_request(req)
    assert raised.value.error_code == "PROVIDER_QUEUE_TIMEOUT"


def _stop_waiting_threads(threads, jobs):
    for ident in threads:
        set_interrupt(True, ident)
    for job in jobs:
        job.result(timeout=1)


# LLM: 真实不可重入锁上的同线程析构在隔离子进程执行；超时转业务失败，不能把pytest主进程锁死。
# 函数用途: 验证延迟归还由下次申请或归还排空，重复归还不会多减。
@pytest.mark.parametrize("drain_by", ["enqueue", "drop"])
def test_gc_finalizer_under_same_thread_lock_is_nonblocking_and_drained(drain_by):
    code = '''
import sys
from types import SimpleNamespace
from agent_py_agent.agent.backends import request_scope as scope
req = SimpleNamespace(api_base="http://gc-queue.invalid", provider_max_concurrent_requests=2)
first = scope.acquire_provider_request(req)
second = scope.acquire_provider_request(req)
with scope._LOCK:
    first.__del__()
    first.__del__()
    assert not first.ticket.released
if sys.argv[1] == "enqueue":
    third = scope.acquire_provider_request(req)
    assert first.ticket.released
    assert scope.provider_endpoint_counts(req) == (2, 0)
    third.release()
else:
    second.release()
    assert first.ticket.released
    assert scope.provider_endpoint_counts(req) == (0, 0)
assert not scope._DEFERRED_RELEASES
first.release()
second.release()
assert scope.provider_endpoint_counts(req) == (0, 0)
'''
    try:
        result = subprocess.run([sys.executable, "-c", code, drain_by], capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail("同线程持有物理请求锁时析构阻塞，未能立即返回")
    assert result.returncode == 0, result.stdout + result.stderr


# LLM: 观察真实Event等待时长；长等待下仍以真实停止/授予置位唤醒，恢复0.1秒轮询会被时长断言拦住。
# 函数用途: 验证无短轮询且两种唤醒在0.2秒内结束，不把仅调用停止当成功。
@pytest.mark.parametrize("action", ["stop", "grant"])
def test_wait_uses_event_not_short_polling_and_wakes_promptly(monkeypatch, action):
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_NOTICE_SECONDS", 60.0)
    req = request(1)
    held = scope.acquire_provider_request(req)
    wake = _ObservedWake()
    original = scope._QueueTicket
    monkeypatch.setattr(scope, "_QueueTicket", lambda owner: original(owner, wake=wake))
    errors = []

    def run():
        with register_interruptible("event-only-wait"):
            try:
                scope.acquire_provider_request(req).release()
            except BaseException as exc:
                errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert wake.entered.wait(1)
        assert wake.timeouts[0] >= 9.0, "等待者仍依赖短轮询而不是Event"
        if action == "stop":
            set_interrupt(True, thread.ident)
        else:
            held.release()
        thread.join(0.2)
        assert not thread.is_alive(), "停止/授予未在0.2秒内唤醒"
        assert (len(errors) == 1 and isinstance(errors[0], InterruptedError)) if action == "stop" else not errors
    finally:
        set_interrupt(True, thread.ident)
        held.release()
        thread.join(1)
    assert scope.provider_endpoint_counts(req) == (0, 0)


# LLM: 保留真实Event置位/清理语义，只公开wait参数，避免替身自身提供停止能力。
# 类用途: 观察生产等待者是否仍使用高频轮询。
class _ObservedWake(threading.Event):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        self.entered.set()
        return super().wait(timeout)


def test_smaller_nonzero_limit_stays_until_endpoint_drains():
    req = request(2)
    first, second = scope.acquire_provider_request(req), scope.acquire_provider_request(req)
    with ExitStack() as stack:
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        stack.callback(first.release)
        stack.callback(second.release)
        lower = pool.submit(scope.acquire_provider_request, request(1))
        wait_until(lambda: scope.provider_endpoint_counts(req) == (2, 1))
        first.release()
        assert scope.provider_endpoint_counts(req) == (1, 1) and not lower.done()
        second.release()
        held = lower.result(timeout=2)
        stack.callback(held.release)
        higher = pool.submit(scope.acquire_provider_request, request(2))
        wait_until(lambda: scope.provider_endpoint_counts(req) == (1, 1))
        held.release()
        higher.result(timeout=2).release()
    assert scope.provider_endpoint_counts(req) == (0, 0)
    first, second = scope.acquire_provider_request(req), scope.acquire_provider_request(req)
    assert scope.provider_endpoint_counts(req) == (2, 0)
    first.release()
    second.release()
