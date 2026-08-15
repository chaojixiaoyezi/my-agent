"""全局并发限制(Tier 3 稳定性闸):在途 LLM 调用数封顶,防打爆 provider 的 RPM/TPM 限额。

provider(Anthropic/OpenAI 等)有硬性并发/速率上限,超了直接 429。worker 池可能 N 副本 ×M 线程
一起冲 → 必须有个全局闸把同时在途的调用数压在安全线内。用有界信号量(自建,stdlib threading):
slot() 上下文管理器拿一个槽,出作用域自动还;槽满则按 timeout 等待,等不到抛 ConcurrencyTimeout
(上层可排队/降级/拒绝,而不是闷等)。

跨实例的全局并发(N 副本共享同一 provider 配额)同样需 Redis/分布式信号量——单实例内本类是权威;
多副本时把总配额按副本数均分,或后续换分布式后端。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ConcurrencyTimeout(Exception):
    """等不到并发槽(在途调用已达上限且超时)。上层应排队/降级,不是闷等。"""


class ConcurrencyLimiter:
    """有界信号量封装:最多 max_in_flight 个并发持槽。slot() 拿槽,出作用域自动还。"""

    def __init__(self, max_in_flight: int) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight 必须 ≥ 1")
        self._max = int(max_in_flight)
        self._sem = threading.BoundedSemaphore(self._max)
        self._in_flight = 0
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, timeout: float | None = None) -> Iterator[None]:
        """拿一个并发槽(满则等 timeout 秒;None=阻塞等)。等不到抛 ConcurrencyTimeout。"""
        got = self._sem.acquire(timeout=timeout) if timeout is not None else self._sem.acquire()
        if not got:
            raise ConcurrencyTimeout(f"等不到并发槽(上限 {self._max},等待 {timeout}s 超时)")
        self._adjust(1)
        try:
            yield
        finally:
            self._adjust(-1)
            self._sem.release()

    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def _adjust(self, delta: int) -> None:
        with self._lock:
            self._in_flight += delta
