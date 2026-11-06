"""进程内可丢失的时间提示；回调只有 Event.set，执行权仍在原事务。"""
from __future__ import annotations

import heapq
import math
import threading
import time

# 墙钟与单调时钟的偏移跨过此不变量边界时，重算全部活跃闹钟。
TIMER_WALL_CLOCK_JUMP_SECONDS = 1.0


class TimerService:
    """一个叶锁、一条守护线程；不在锁内调用外部对象。"""

    def __init__(self, *, resample_seconds: float):
        if not math.isfinite(resample_seconds) or resample_seconds <= 0:
            raise ValueError("resample_seconds must be positive and finite")
        self.resample_seconds = float(resample_seconds)
        self._condition = threading.Condition()
        self._heap = []
        self._live = {}
        self._firing = {}
        self._events = {}
        self._generation = 0
        self._offset = time.time() - time.monotonic()
        self._sample_at = time.monotonic() + self.resample_seconds
        self._last_drift = 0.0
        self._stopping = False
        self._thread = None
        self._callback_failures = 0

    def schedule(self, key, due_wall: float | None) -> int:
        if due_wall is not None and not math.isfinite(due_wall):
            raise ValueError("deadline must be finite or None")
        with self._condition:
            # 全局序号也严格递增于每个key，不必永久保存已取消key的墓碑。
            self._generation += 1
            generation = self._generation
            self._firing.pop(key, None)
            if due_wall is None:
                self._live.pop(key, None)
            else:
                wall = float(due_wall)
                self._live[key] = (wall, generation)
                heapq.heappush(self._heap, (wall - self._offset, generation, generation, key))
            self._compact_locked()
            self._condition.notify_all()
            return generation

    def cancel(self, key) -> int:
        return self.schedule(key, None)

    def bind(self, kind: str, wake_event: threading.Event) -> None:
        with self._condition:
            self._events[kind] = wake_event

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="owner-timer", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        with self._condition:
            self._stopping = True
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "live_keys": len(self._live), "heap_entries": len(self._heap),
                "stale_entries": len(self._heap) - len(self._live),
                "last_drift": self._last_drift, "generation": self._generation,
                "callback_failures": self._callback_failures,
                "running": self._thread is not None and self._thread.is_alive(),
            }

    def _compact_locked(self) -> None:
        if len(self._heap) > 2 * len(self._live):
            self._heap = [(wall - self._offset, generation, generation, key)
                          for key, (wall, generation) in self._live.items()]
            heapq.heapify(self._heap)

    def _resample_locked(self, now: float) -> None:
        if now < self._sample_at:
            return
        offset = time.time() - now
        drift = offset - self._offset
        self._sample_at = now + self.resample_seconds
        if abs(drift) > TIMER_WALL_CLOCK_JUMP_SECONDS:
            self._last_drift = drift
            self._offset = offset
            self._heap = [(wall - offset, generation, generation, key)
                          for key, (wall, generation) in self._live.items()]
            heapq.heapify(self._heap)

    def _take_due_locked(self, now: float) -> list:
        ready = []
        while self._heap and self._heap[0][0] <= now:
            _due, generation, _seq, key = heapq.heappop(self._heap)
            live = self._live.get(key)
            if live is None or live[1] != generation:
                continue
            del self._live[key]
            self._firing[key] = generation
            ready.append((key, generation))
        self._compact_locked()
        return ready

    def _dispatch(self, ready: list) -> None:
        for key, generation in ready:
            with self._condition:
                if self._stopping or self._firing.get(key) != generation:
                    continue
                del self._firing[key]
                event = self._events.get(key[1])
            # 锁外只发提示；即使消费者已先重排，提示也不能消费旧行。
            if event is not None:
                try:
                    event.set()
                except Exception:
                    with self._condition:
                        self._callback_failures += 1

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                now = time.monotonic()
                self._resample_locked(now)
                ready = self._take_due_locked(now)
                if not ready:
                    wait = max(0.0, self._sample_at - now)
                    if self._heap:
                        wait = min(wait, max(0.0, self._heap[0][0] - now))
                    self._condition.wait(wait)
                    continue
            self._dispatch(ready)
