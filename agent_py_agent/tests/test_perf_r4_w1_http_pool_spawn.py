# LLM: PERF-R4-W1 回归：CPython 3.13+ 的 _worker 只在取空队列时归还空闲令牌，LIFO 唤醒下令牌少计让 HTTP 池持续扩容到
#   max_workers（线上 17→128 个 gateway-http 线程，每个线程保留自身 mimalloc 峰值）；有空闲 worker 时提交不得再起线程，
#   工作不丢不饿死，全忙时仍按原规则扩容。
# 模块用途: 固定 DurableDaemonThreadPoolExecutor + _CancellableWorkQueue 的扩容判定，不起 Gateway、不触网。
from __future__ import annotations

import threading
import time

from agent_py_agent.agent.concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
from agent_py_agent.agent.gateway_parts.http_lanes import _CancellableWorkQueue


# 函数用途: 建立与 HTTP 车道相同队列类型的执行器。
def _pool(workers: int) -> DurableDaemonThreadPoolExecutor:
    executor = DurableDaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix="perf-r4-w1")
    executor._work_queue = _CancellableWorkQueue()
    return executor


# 函数用途: 等到指定个数的 worker 挂在队列条件变量上。
def _wait_parked(queue: _CancellableWorkQueue, count: int = 1, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with queue.mutex:
            if len(queue.not_empty._waiters) >= count:
                return True
        time.sleep(0.005)
    return False


def test_idle_worker_serves_submit_even_after_idle_token_was_lost():
    executor = _pool(8)
    try:
        assert executor.submit(lambda: "first").result(timeout=5) == "first"
        assert _wait_parked(executor._work_queue)
        # 复现线上少计状态：令牌已被消耗，但那个 worker 仍挂起空闲。
        while executor._idle_semaphore.acquire(timeout=0):
            pass
        assert executor.submit(lambda: "served").result(timeout=5) == "served"
        assert len(executor._threads) == 1
    finally:
        executor.shutdown(wait=False)


def test_pool_still_grows_while_every_worker_is_busy():
    executor = _pool(4)
    gate = threading.Event()
    try:
        futures = [executor.submit(gate.wait, 5) for _ in range(3)]
        deadline = time.monotonic() + 5
        while len(executor._threads) < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(executor._threads) == 3
        # 三个 worker 都被长任务占住时，再来一项必须扩容而不是排在长任务后面。
        extra = executor.submit(lambda: "extra")
        assert extra.result(timeout=5) == "extra"
        assert len(executor._threads) == 4
        gate.set()
        assert all(future.result(timeout=5) for future in futures)
    finally:
        gate.set()
        executor.shutdown(wait=False)


def test_steady_low_concurrency_pool_plateaus_instead_of_creeping():
    executor = _pool(128)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def task() -> None:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.004)
        with lock:
            state["now"] -= 1

    try:
        futures, warm = [], 0
        for index in range(900):
            futures.append(executor.submit(task))
            if index == 299:
                warm = len(executor._threads)
            time.sleep(0.002)
        for future in futures:
            future.result(timeout=10)
        # 预热后不再随时间增长（旧实现约每秒多起数个线程，60 秒到 90+），总量也只比峰值并发多几个过渡中的 worker。
        assert len(executor._threads) - warm <= 3, (warm, len(executor._threads), state["peak"])
        assert len(executor._threads) <= 3 * state["peak"] + 8, (len(executor._threads), state["peak"])
    finally:
        executor.shutdown(wait=False)
