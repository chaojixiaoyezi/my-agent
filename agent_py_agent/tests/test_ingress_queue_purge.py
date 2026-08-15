"""审计 #16(part A,medium/稳定)真测:ingress 队列旧行 TTL 清理 + claim 复合索引。

无界增长根因:ingress_messages 表无 TTL,completed/failed 墓碑行永久保留,长跑数周必胀;且除
dedup_key 外零索引,claim 扫描随表线性退化、PG 上 autovacuum 膨胀。真 SQLite:终结行超保留期
被清、在途/新近行绝不动,复合索引真落库且幂等补回。注入 now_ms 确定性。学 通道运行时 WAL maintenance。
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
from sqlalchemy import inspect, text  # noqa: E402

from agent_py_agent.agent.ingress_queue import IngressQueue  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402

_DAY_MS = 86_400_000


def _q(tmp_path) -> IngressQueue:
    q = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"))
    q.ensure_schema()
    return q


def _enqueue_and_complete(q: IngressQueue, key: str, lane: str, now_ms: int) -> None:
    q.enqueue(key, lane, {}, now_ms=now_ms)
    msg = q.claim(now_ms=now_ms)
    assert msg is not None
    q.complete(msg.claim_token)


def test_purge_removes_old_terminal_keeps_recent_and_inflight(tmp_path) -> None:
    q = _q(tmp_path)
    now = 100 * _DAY_MS
    _enqueue_and_complete(q, "old-done", "L1", now - 30 * _DAY_MS)  # 老·已完成 → 删
    q.enqueue("old-fail", "L2", {}, now_ms=now - 30 * _DAY_MS)  # 老·死信 → 删
    mf = q.claim(now_ms=now - 30 * _DAY_MS)
    assert mf is not None
    q.fail(mf.claim_token, retryable=False, error="poison", now_ms=now - 30 * _DAY_MS)
    _enqueue_and_complete(q, "recent-done", "L3", now - 1 * _DAY_MS)  # 近·已完成 → 保留
    q.enqueue("inflight", "L4", {}, now_ms=now)  # 在途 pending → 永不删

    removed = q.purge_old(retention_ms=7 * _DAY_MS, now_ms=now)
    assert removed == 2  # 仅两条超 7 天的终结行被清
    stats = q.stats()
    assert stats.get("completed", 0) == 1  # 近完成保留(墓碑去重窗口内)
    assert stats.get("pending", 0) == 1  # 在途保留
    assert stats.get("failed", 0) == 0  # 老死信已清
    # 墓碑已删 → 老 key 可重新入队(N 天外平台重投极罕见,可接受)
    assert q.enqueue("old-done", "L1", {}, now_ms=now) is True


def test_purge_never_touches_pending_or_claimed(tmp_path) -> None:
    q = _q(tmp_path)
    now = 100 * _DAY_MS
    q.enqueue("p", "L", {}, now_ms=now - 60 * _DAY_MS)  # 很老但 pending
    q.enqueue("c", "L2", {}, now_ms=now - 60 * _DAY_MS)
    assert q.claim(now_ms=now) is not None  # 一条转 claimed(created_at 仍很老)
    removed = q.purge_old(retention_ms=1, now_ms=now)  # 极短保留期也不该误删在途
    assert removed == 0  # pending/claimed 在途行绝不删,哪怕 created_at 很老
    stats = q.stats()
    assert stats.get("pending", 0) == 1 and stats.get("claimed", 0) == 1


def test_purge_batch_limit_caps_one_call(tmp_path) -> None:
    q = _q(tmp_path)
    now = 100 * _DAY_MS
    for i in range(5):
        _enqueue_and_complete(q, f"d{i}", f"L{i}", now - 30 * _DAY_MS)
    first = q.purge_old(retention_ms=_DAY_MS, now_ms=now, limit=2)
    assert first == 2  # 分批:单次至多 limit 行(防大事务长锁)
    rest = q.purge_old(retention_ms=_DAY_MS, now_ms=now, limit=100)
    assert rest == 3  # 剩余继续清


def test_claim_index_present_after_ensure_schema(tmp_path) -> None:
    q = _q(tmp_path)
    names = {ix["name"] for ix in inspect(q._backend.engine).get_indexes("ingress_messages")}
    assert "ix_ingress_status_visible" in names  # 复合索引真落库(claim 扫描不再全表)


def test_ensure_schema_recreates_missing_index_idempotent(tmp_path) -> None:
    q = _q(tmp_path)
    with q._backend.begin() as conn:
        conn.execute(text("DROP INDEX ix_ingress_status_visible"))  # 模拟旧库无索引
    q.ensure_schema()  # 幂等:补回缺失索引,不报错(老客户库可安全升级)
    names = {ix["name"] for ix in inspect(q._backend.engine).get_indexes("ingress_messages")}
    assert "ix_ingress_status_visible" in names
