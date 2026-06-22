"""#3 真实压测:IngressQueue + WorkerPool 在高并发多生产者/多 worker 下的契约不变量(真 PG)。

不是单机跑 10 万(单测环境做不到),但真起 8 worker + 4 并发生产者灌 ~800 条跨 40 lane,压真实争用
路径(claim 的 SKIP LOCKED / lane 排他 / 心跳 / complete),断言契约在高并发下成立:
① 精确一次(每条处理且仅一次,不丢不重)② 全部 drain 无残留 ③ 同 lane 串行(不并发)④ 背压(满则 429)。
无 PG(localhost:5432 + psycopg)则 skip。
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")
from sqlalchemy import text  # noqa: E402

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueBackpressure, QueueConfig  # noqa: E402
from agent_py_agent.agent.queue_worker import WorkerPool  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402

_PG_URL = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")


def _fresh_queue(cfg: QueueConfig) -> IngressQueue:
    try:
        backend = StorageBackend(_PG_URL)
        with backend.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS ingress_messages"))
    except Exception as exc:  # pragma: no cover - 无 PG 环境
        pytest.skip(f"无可用 PostgreSQL: {type(exc).__name__}")
    queue = IngressQueue(backend, cfg)
    queue.ensure_schema()
    return queue


def _wait_until(predicate, *, timeout: float = 60.0, poll: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def test_load_exactly_once_and_lane_serial_under_concurrency() -> None:
    queue = _fresh_queue(QueueConfig(global_cap=100_000, lane_cap=10_000, max_attempts=5))
    total, lanes = 800, 40

    processed: list[int] = []
    lane_inflight: dict[str, int] = defaultdict(int)
    lane_max: dict[str, int] = defaultdict(int)
    lock = threading.Lock()

    def handler(payload: dict) -> None:
        lane = str(payload["lane"])
        with lock:
            lane_inflight[lane] += 1
            lane_max[lane] = max(lane_max[lane], lane_inflight[lane])
        time.sleep(0.001)  # 持一会儿,逼真暴露同 lane 并发
        with lock:
            lane_inflight[lane] -= 1
            processed.append(int(payload["mid"]))

    def produce(base: int, count: int) -> None:
        for i in range(base, base + count):
            queue.enqueue(f"msg-{i}", f"lane-{i % lanes}", {"mid": i, "lane": f"lane-{i % lanes}"})

    producers = [threading.Thread(target=produce, args=(p * 200, 200)) for p in range(4)]  # 4×200=800 并发灌
    for t in producers:
        t.start()
    for t in producers:
        t.join()
    assert queue.stats().get("pending") == total  # 800 条都入队(并发生产无丢)

    pool = WorkerPool(queue, handler, workers=8, lease_seconds=30)
    pool.start()
    drained = _wait_until(lambda: len(processed) >= total, timeout=90.0)
    pool.stop()

    assert drained, f"未在超时内 drain:processed={len(processed)}/{total}"
    assert len(processed) == total  # 不丢
    assert len(set(processed)) == total  # ⭐ 精确一次:无重复处理
    assert all(v == 1 for v in lane_max.values())  # ⭐ 同 lane 串行:任一 lane 从不并发
    stats = queue.stats()
    assert stats.get("completed") == total and not stats.get("pending") and not stats.get("claimed")  # 无残留


def test_load_backpressure_rejects_when_full() -> None:
    queue = _fresh_queue(QueueConfig(global_cap=50, lane_cap=50, max_attempts=5))
    accepted = 0
    rejected = 0
    for i in range(80):  # 灌超过 global_cap=50
        try:
            queue.enqueue(f"bp-{i}", "L", {"mid": i})
            accepted += 1
        except QueueBackpressure:
            rejected += 1
    assert accepted == 50 and rejected == 30  # 满 50 后背压拒绝余下(转 429 让平台重投)
