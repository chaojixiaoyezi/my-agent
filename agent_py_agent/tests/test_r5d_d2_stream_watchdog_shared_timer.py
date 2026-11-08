"""D-1/D2: model-stream idle watchdogs share one deferred-call thread instead of one threading.Timer per stream."""
from __future__ import annotations

import threading
import time

from agent_py_agent.agent.backends.gateway_helpers import _StreamIdleWatchdog

# 同时在途的模型流条数（每条一只 watchdog）。
STREAM_COUNT = 12
# 单只 watchdog 的空闲期限（秒）。
IDLE_SECONDS = 0.2
# 等待中止或确认未中止的上限（秒）。
WAIT_SECONDS = 2.0


# 类用途: 记录 watchdog 是否请求中止当前响应。
class _RecordingGuard:
    def __init__(self) -> None:
        self.aborted = threading.Event()

    # 函数用途: 记录一次中止请求。
    def abort(self) -> None:
        self.aborted.set()


# 函数用途: 建一只只有空闲期限的 watchdog。
def _watchdog(idle: float, timeout: float = 300.0):
    guard = _RecordingGuard()
    return _StreamIdleWatchdog(guard, timeout, idle_deadline=time.monotonic() + idle), guard


# 函数用途: 多条在途流的 watchdog 不再各起一条线程；每只仍按自己的期限中止自己的响应。
def test_concurrent_stream_watchdogs_do_not_start_a_thread_each(monkeypatch):
    starts = []
    original_start = threading.Thread.start

    def counting_start(self):
        starts.append(self.name)
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", counting_start)
    pairs = [_watchdog(IDLE_SECONDS + index * 0.01) for index in range(STREAM_COUNT)]
    try:
        for watchdog, _guard in pairs:
            watchdog.start()
        for watchdog, guard in pairs:
            assert guard.aborted.wait(WAIT_SECONDS), "到期的 watchdog 必须中止自己的响应"
            assert watchdog.timed_out and watchdog.timeout_stage == "first_event"
    finally:
        for watchdog, _guard in pairs:
            watchdog.cancel()
    assert len(starts) <= 1, f"{STREAM_COUNT} 只 watchdog 最多懒启动一条共用线程，实际新建 {len(starts)} 条: {starts}"


# 函数用途: touch 滚动续期仍有效：首个期限到点时发现已续期就改排到新期限，之后才中止，阶段记为流空闲。
def test_touch_rolls_the_idle_deadline_on_the_shared_thread():
    watchdog, guard = _watchdog(IDLE_SECONDS, timeout=0.4)
    watchdog.start()
    try:
        time.sleep(IDLE_SECONDS / 2)
        watchdog.touch()
        assert not guard.aborted.wait(IDLE_SECONDS), "touch 之后原期限不能中止"
        assert guard.aborted.wait(WAIT_SECONDS), "续期后的空闲期限仍须中止"
        assert watchdog.timed_out and watchdog.timeout_stage == "stream_idle"
    finally:
        watchdog.cancel()


# 函数用途: cancel 之后到期回调不再中止，也不记超时；同一共用线程继续服务其它 watchdog。
def test_cancelled_watchdog_never_aborts_and_others_still_fire():
    cancelled, cancelled_guard = _watchdog(IDLE_SECONDS)
    live, live_guard = _watchdog(IDLE_SECONDS * 2)
    cancelled.start()
    live.start()
    cancelled.cancel()
    try:
        assert live_guard.aborted.wait(WAIT_SECONDS)
        assert not cancelled_guard.aborted.is_set() and not cancelled.timed_out
    finally:
        live.cancel()
