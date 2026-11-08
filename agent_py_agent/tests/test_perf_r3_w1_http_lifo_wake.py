# LLM: PERF-R3-W1 回归：HTTP/业务流工作队列必须让最近空闲的线程接手新请求，短请求不得在整池线程间轮转
#   （3.14t mimalloc 按线程保留峰值，轮转会让 128 个工作线程各自长到峰值）；条目出队顺序与丢弃语义保持原样。
# 模块用途: 固定 _CancellableWorkQueue 的唤醒顺序、池内线程集中度与 FIFO/丢弃合同，不起 Gateway、不触网。
from __future__ import annotations

import threading
import time

from agent_py_agent.agent.concurrency import DurableDaemonThreadPoolExecutor
from agent_py_agent.agent.gateway_parts.http_lanes import _CancellableWorkQueue


# 函数用途: 等待条件成立（最多 5 秒），避免依赖固定睡眠。
def _until(predicate) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_put_wakes_the_most_recent_waiter():
    queue = _CancellableWorkQueue()
    got: dict[str, object] = {}

    # 函数用途: 阻塞取一项并记录是哪个消费者拿到。
    def consume(name: str) -> None:
        item = queue.get(timeout=5)
        got[name] = item

    threads = []
    for index, name in enumerate(("oldest", "middle", "newest")):
        thread = threading.Thread(target=consume, args=(name,), daemon=True)
        thread.start()
        threads.append(thread)
        _until(lambda count=index + 1: len(queue.not_empty._waiters) == count)
    queue.put("work")
    _until(lambda: got)
    assert got == {"newest": "work"}
    queue.put(None)
    queue.put(None)
    for thread in threads:
        thread.join(timeout=5)
    assert set(got) == {"newest", "middle", "oldest"}


def test_pool_serves_sequential_requests_from_one_hot_thread_after_a_burst():
    executor = DurableDaemonThreadPoolExecutor(max_workers=16, thread_name_prefix="perf-r3-w1")
    executor._work_queue = _CancellableWorkQueue()
    try:
        gate = threading.Event()
        burst = [executor.submit(gate.wait, 5) for _ in range(16)]
        _until(lambda: len(executor._threads) == 16)
        gate.set()
        for future in burst:
            future.result(timeout=5)
        names = []
        for _ in range(48):
            # 每次提交前等全部 16 个线程重新挂到等待表：自由线程下刚完成的 worker 可能尚未回到 get()，不能让这个时序决定结果。
            _until(lambda: len(executor._work_queue.not_empty._waiters) == 16)
            names.append(executor.submit(lambda: threading.current_thread().name).result(timeout=5))
        # FIFO 唤醒会把 48 个顺序请求轮转到全部 16 个线程；最近空闲优先只需一个热线程。
        assert len(set(names)) == 1
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def test_items_stay_fifo_and_discard_still_removes_queued_work():
    queue = _CancellableWorkQueue()
    for item in ("a", "b", "c"):
        queue.put(item)
    assert [queue.get_nowait() for _ in range(3)] == ["a", "b", "c"]

    # 类用途: 带 future 属性的排队项替身，与 discard 的匹配字段一致。
    class _Item:
        def __init__(self, future):
            self.future, self.args = future, ()

    owner, other = object(), object()
    queue.put(_Item(other))
    queue.put(_Item(owner))
    queue.discard(owner)
    assert queue.qsize() == 1 and queue.get_nowait().future is other
