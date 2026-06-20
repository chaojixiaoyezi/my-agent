"""Tier 1.2 入站队列测试:墓碑去重/lane 串行/lease 恢复/毒丸/背压。SQLite 与真 PostgreSQL 双跑。

时间用可注入的 now_ms 确定性控制(不靠真 sleep)。PG 真测验 SKIP-LOCKED 路径。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueBackpressure, QueueConfig  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def _pg_backend() -> StorageBackend:
    from sqlalchemy import text

    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url)
        with db.connect() as conn:
            conn.execute(text("SELECT 1"))
        return db
    except Exception as exc:
        pytest.skip(f"无可用 PostgreSQL: {type(exc).__name__}")


@pytest.fixture(params=["sqlite", "postgres"])
def queue(request):
    backend = StorageBackend.in_memory() if request.param == "sqlite" else _pg_backend()
    q = IngressQueue(backend, QueueConfig(global_cap=100, lane_cap=3, max_attempts=2))
    q._meta.drop_all(backend.engine)  # 干净起点(PG 表持久,清掉)
    q.ensure_schema()
    yield q
    try:
        q._meta.drop_all(backend.engine)
    finally:
        backend.dispose()


def test_tombstone_dedup(queue) -> None:
    assert queue.enqueue("evt-1", "lane-a", {"x": 1}) is True
    assert queue.enqueue("evt-1", "lane-a", {"x": 2}) is False  # 同 dedup_key → 去重(防平台重投重复处理)
    assert queue.stats().get("pending") == 1


def test_claim_marks_and_returns_payload(queue) -> None:
    queue.enqueue("e1", "L", {"msg": "你好"})
    claimed = queue.claim()
    assert claimed is not None
    assert claimed.payload == {"msg": "你好"} and claimed.attempts == 1
    assert queue.stats().get("claimed") == 1


def test_lane_serial_same_lane_blocks_until_complete(queue) -> None:
    queue.enqueue("e1", "session-A", {"n": 1})
    queue.enqueue("e2", "session-A", {"n": 2})
    first = queue.claim()
    assert first is not None and first.payload["n"] == 1
    assert queue.claim() is None  # 同 lane 有在飞 → 不再领(会话串行)
    assert queue.complete(first.claim_token) is True
    second = queue.claim()
    assert second is not None and second.payload["n"] == 2  # 上一条完成后才领下一条


def test_different_lanes_parallel(queue) -> None:
    queue.enqueue("a", "lane-A", {})
    queue.enqueue("b", "lane-B", {})
    c1 = queue.claim()
    c2 = queue.claim()
    assert c1 is not None and c2 is not None  # 不同 lane 可并行领
    assert {c1.lane, c2.lane} == {"lane-A", "lane-B"}


def test_lease_recovery_requeues_then_poison_fails(queue) -> None:
    queue.enqueue("e1", "L", {}, now_ms=1000)
    c1 = queue.claim(lease_seconds=10, now_ms=1000)  # lease 到 11000,attempts=1
    assert c1 is not None
    # 时间推到 lease 过期后:recover_stale 退回 pending(worker 崩→消息不卡死)
    assert queue.recover_stale(now_ms=20000) == 1
    assert queue.stats().get("pending") == 1
    # 再领→attempts=2(达 max_attempts=2);再过期→毒丸保护转 failed,不无限重试
    c2 = queue.claim(lease_seconds=10, now_ms=20000)
    assert c2 is not None and c2.attempts == 2
    assert queue.recover_stale(now_ms=40000) == 1
    assert queue.stats().get("failed") == 1 and queue.stats().get("pending", 0) == 0


def test_heartbeat_extends_lease_prevents_recovery(queue) -> None:
    queue.enqueue("e1", "L", {}, now_ms=1000)
    c1 = queue.claim(lease_seconds=10, now_ms=1000)  # lease 到 11000
    assert queue.heartbeat(c1.claim_token, lease_seconds=100, now_ms=10000) is True  # 续到 110000
    assert queue.recover_stale(now_ms=20000) == 0  # 已续租 → 不被回收(多步 LLM turn 不被重复处理)


def test_backpressure_per_lane_cap(queue) -> None:
    for i in range(3):  # lane_cap=3
        assert queue.enqueue(f"e{i}", "hot-lane", {}) is True
    with pytest.raises(QueueBackpressure):  # 第 4 条超 per-lane 上限 → 429
        queue.enqueue("e3", "hot-lane", {})
