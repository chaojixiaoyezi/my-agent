"""只验证原轮询线程的独立消费与退出责任，不启动真正W或native控制。"""
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_control_poller import ControlPollingLane


def test_independent_poll_lane_is_joined_after_stop():
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    threads = []

    # LLM: 此替身只有同步poll观察，不造控制回执、owner、权限或完成状态。
    # 函数用途: 保持原轮询线程活到测试明确释放，验证close真的join该线程。
    def poll():
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(3)

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    try:
        lane.start()
        assert entered.wait(3)
        assert threads == [lane._thread] and lane._thread is not threading.current_thread()
        assert not lane._thread.daemon
        stop.set()
    finally:
        release.set()
        lane.close()
    assert not lane._thread.is_alive()


def test_typed_poll_failure_wakes_worker_and_keeps_original_exception():
    stop = threading.Event()
    original = RequestExecutionContextUnavailable("synthetic_source_expired")
    calls = []

    # LLM: 原typed对象在真实线程里抛出，不模拟side effect或request unknown提交。
    # 函数用途: 核轮询异常唤醒主循环并由同一close传播，不能静默重试。
    def poll():
        calls.append(True)
        raise original

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    lane.start()
    assert stop.wait(3)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        lane.close()
    assert caught.value is original and calls == [True]
    assert not lane._thread.is_alive()


def test_already_stopping_worker_never_enters_poll():
    stop = threading.Event()
    stop.set()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: pytest.fail("stopped worker polled")), stop)
    lane.start()
    lane.close()
    assert not lane._thread.is_alive()


def test_constructor_preserves_thread_before_any_start_attempt(monkeypatch):
    def forbidden_start(_thread):
        pytest.fail("construction started the original thread")

    monkeypatch.setattr(threading.Thread, "start", forbidden_start)
    stop = threading.Event()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: None), stop)
    assert lane._thread.ident is None
    assert not stop.is_set()
    lane.close()


def test_started_thread_is_retained_when_start_ack_raises(monkeypatch):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    real_start = threading.Thread.start
    original = RuntimeError("synthetic_start_ack_error")

    def poll():
        entered.set()
        assert release.wait(3)

    def start_then_raise(thread):
        real_start(thread)
        assert entered.wait(3)
        raise original

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    thread = lane._thread
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)
    try:
        with pytest.raises(RuntimeError) as caught:
            lane.start()
        assert caught.value is original
        assert stop.is_set() and lane._thread is thread and thread.is_alive()
        with pytest.raises(RuntimeError):
            lane.start()
    finally:
        release.set()
        lane.close()
    assert lane._thread is thread and not thread.is_alive()


def test_unknown_birth_is_not_retried_or_reported_as_joined(monkeypatch):
    original = RuntimeError("synthetic_before_birth_error")
    calls = []

    def start_without_ack(thread):
        calls.append(thread)
        raise original

    stop = threading.Event()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: None), stop)
    thread = lane._thread
    monkeypatch.setattr(threading.Thread, "start", start_without_ack)
    try:
        with pytest.raises(RuntimeError) as caught:
            lane.start()
        assert caught.value is original and stop.is_set()
        with pytest.raises(RuntimeError):
            lane.start()
        with pytest.raises(RuntimeError, match="cannot join thread before it is started"):
            lane.close()
        assert calls == [thread] and lane._thread is thread
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(3)
            assert not thread.is_alive()


def test_worker_root_keeps_lane_for_finally_after_start_ack_error(monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker

    entered, release = threading.Event(), threading.Event()
    real_start, real_close = threading.Thread.start, ControlPollingLane.close
    original = RuntimeError("synthetic_root_start_ack_error")
    threads, closed, claim_stops, pump_closes, notices = [], [], [], [], []
    partition = object()
    launch = SimpleNamespace(partition=partition, parent_pid=1, instance_id="")

    def poll():
        entered.set()
        assert release.wait(3)

    def start_then_raise(thread):
        threads.append(thread)
        real_start(thread)
        assert entered.wait(3)
        raise original

    def close_original(lane):
        closed.append(lane._thread)
        assert lane.worker_stop.is_set()
        release.set()
        real_close(lane)

    pump = SimpleNamespace(controls=SimpleNamespace(poll=poll), inflight=0,
                           stop_claiming=lambda: claim_stops.append(True),
                           close=lambda: pump_closes.append(True))
    monkeypatch.setattr(worker, "_install_stop_handlers", lambda _stop: {})
    monkeypatch.setattr(worker, "_restore_stop_handlers", lambda _previous: None)
    monkeypatch.setattr(worker, "_arm_parent_exit", lambda _pid: "")
    monkeypatch.setattr(worker, "prepare_worker_readiness", lambda _launch: {"ready": True})
    monkeypatch.setattr(worker, "current_worker_partition", lambda: partition)
    monkeypatch.setattr(worker, "create_request_pump", lambda *_args: pump)
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda _launch, row, _notice: notices.append(row))
    monkeypatch.setattr(worker, "_worker_execution_loop", lambda *_args: pytest.fail("failed start reached request loop"))
    monkeypatch.setattr(ControlPollingLane, "close", close_original)
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)
    try:
        with pytest.raises(RuntimeError) as caught:
            worker.run_worker(launch)
        assert caught.value is original
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
    assert len(threads) == 1 and closed == threads
    assert claim_stops == [True] and pump_closes == [True]
    assert notices[-1]["reason"] == "worker_runtime_failed"
