"""Tier 1.1 worker 测试:run_once 处理/handler 异常→fail/WorkerPool 并发消费全处理。

用 SQLite in-memory(StaticPool 单连接共享,跨线程持久)验证多 worker 线程并发消费同一队列。
"""

from __future__ import annotations

import threading
import time

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
from agent_py_agent.agent.queue_worker import QueueWorker, WorkerPool
from agent_py_agent.agent.storage_backend import StorageBackend


def _queue() -> IngressQueue:
    q = IngressQueue(StorageBackend.in_memory(), QueueConfig(lane_cap=50))
    q.ensure_schema()
    return q


def test_run_once_processes_and_completes() -> None:
    q = _queue()
    seen: list[dict] = []
    q.enqueue("e1", "L", {"msg": "hi"})
    worker = QueueWorker(q, seen.append)
    assert worker.run_once() is True
    assert seen == [{"msg": "hi"}]  # handler 收到 payload
    assert q.stats().get("completed") == 1


def test_run_once_empty_returns_false() -> None:
    assert QueueWorker(_queue(), lambda _p: None).run_once() is False


def test_handler_exception_requeues_for_retry_not_stuck() -> None:
    q = _queue()
    q.enqueue("e1", "L", {})

    def boom(_payload: dict) -> None:
        raise RuntimeError("handler 炸了")

    assert QueueWorker(q, boom).run_once() is True
    # #17:可重试失败 → 退避重投(回 pending),不卡 lane、不一次就死信
    assert q.stats().get("claimed", 0) == 0
    assert q.stats().get("pending") == 1  # 重投待重试
    assert q.stats().get("failed", 0) == 0


def test_worker_pool_concurrently_processes_all(tmp_path) -> None:
    # 文件 SQLite(WAL,每 worker 线程独立连接,BEGIN IMMEDIATE 串行写)——in-memory 单连接扛不住并发
    q = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"), QueueConfig(lane_cap=50))
    q.ensure_schema()
    processed: list[dict] = []
    lock = threading.Lock()

    def handler(payload: dict) -> None:
        with lock:
            processed.append(payload)

    for i in range(8):  # 不同 lane → 可并行领
        q.enqueue(f"e{i}", f"lane-{i}", {"n": i})
    pool = WorkerPool(q, handler, workers=4)
    pool.start()
    try:
        deadline = time.monotonic() + 5.0
        while q.stats().get("completed", 0) < 8 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pool.stop()
    assert q.stats().get("completed") == 8  # 8 条全被 4 worker 并发消费完
    assert sorted(p["n"] for p in processed) == list(range(8))
