"""堆定时器的运行时合同；不用源码扫描充当并发验证。"""
from __future__ import annotations

import random
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.scheduler import timer_service as module
from agent_py_agent.agent.scheduler.timer_service import TimerService


def test_schedule_keeps_live_key():
    timer = TimerService(resample_seconds=1)
    assert timer.schedule(("alice", "scheduler_due"), time.time() + 1) > 0
    assert timer.snapshot()["live_keys"] == 1


def test_ten_thousand_keys_preserve_due_order(monkeypatch):
    clock = SimpleNamespace(wall=100.0, mono=1.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono))
    timer = TimerService(resample_seconds=1)
    order = list(range(10_000))
    random.Random(4).shuffle(order)
    for value in order:
        timer.schedule((value, "scheduler_due"), 101 + value / 10_000)
    clock.mono = 4.0
    with timer._condition:
        ready = timer._take_due_locked(clock.mono)
    assert [key[0] for key, _generation in ready] == list(range(10_000))
    assert timer.snapshot()["heap_entries"] == 0


def test_cancel_reschedule_and_rebuild_invalidate_popped_generation():
    timer = TimerService(resample_seconds=1)
    event = threading.Event()
    timer.bind("runtime_wake", event)
    key = ("alice", "runtime_wake")
    first = timer.schedule(key, time.time() - 1)
    with timer._condition:
        old = timer._take_due_locked(time.monotonic())
    cancelled = timer.cancel(key)
    newest = timer.schedule(key, time.time() - 1)
    assert first < cancelled < newest
    timer._dispatch(old)
    assert not event.is_set()
    with timer._condition:
        ready = timer._take_due_locked(time.monotonic())
    timer._dispatch(ready)
    assert event.is_set()


def test_mass_reschedules_keep_heap_bounded():
    timer = TimerService(resample_seconds=1)
    for cycle in range(20):
        for owner in range(400):
            timer.schedule((owner, "maintenance"), time.time() + cycle + 10)
        snapshot = timer.snapshot()
        assert snapshot["stale_entries"] <= snapshot["live_keys"]
        assert snapshot["heap_entries"] <= 2 * snapshot["live_keys"]
    for owner in range(400):
        timer.cancel((owner, "maintenance"))
    assert timer.snapshot()["heap_entries"] == 0


@pytest.mark.parametrize("jump", [100.0, -100.0])
def test_wall_clock_jump_during_wait_is_resampled(monkeypatch, jump):
    wall = time.time
    offset = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: wall() + offset[0], monotonic=time.monotonic))
    timer = TimerService(resample_seconds=0.015)
    event = threading.Event()
    timer.bind("runtime_wake", event)
    timer.schedule(("a", "runtime_wake"), wall() + (10 if jump > 0 else 0.08))
    timer.start()
    try:
        offset[0] = jump
        deadline = time.monotonic() + 0.4
        while abs(timer.snapshot()["last_drift"]) < 1 and time.monotonic() < deadline:
            event.wait(0.005)
        assert timer.snapshot()["last_drift"] == pytest.approx(jump, abs=0.1)
        assert event.wait(0.03) is (jump > 0)
    finally:
        timer.stop(0.5)


def test_stop_interrupts_long_wait_and_callback_is_outside_lock():
    timer = TimerService(resample_seconds=30)
    calls = []
    fired = threading.Event()

    class CheckingEvent:
        def set(self):
            calls.append(timer._condition._is_owned())
            fired.set()

    timer.bind("scheduler_due", CheckingEvent())
    timer.schedule(("a", "scheduler_due"), time.time() + 0.02)
    timer.start()
    assert fired.wait(0.5)
    assert calls == [False]
    timer.schedule(("b", "scheduler_due"), time.time() + 1000)
    started = time.monotonic()
    timer.stop(0.2)
    assert time.monotonic() - started < 0.2
    assert not timer.snapshot()["running"]
