"""审计 #17 修复真测:队列 handler 级退避重试 + 死信队列 + 退避延迟可见 + 旧表幂等补列。

真 SQLite:可重试失败退避重投(回 pending,退避期内不可领),达上限进死信(failed),requeue_dead 重放;
旧表(无 next_visible_at/last_error)ensure_schema 幂等补列不丢数据。注入 now_ms 确定性。
学 Celery acks_late+retry_backoff / Sidekiq dead set。
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")
from sqlalchemy import text  # noqa: E402

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def _q(tmp_path, *, max_attempts: int = 5) -> IngressQueue:
    q = IngressQueue(StorageBackend.for_path(tmp_path / "q.db"), QueueConfig(max_attempts=max_attempts))
    q.ensure_schema()
    return q


def test_retryable_failure_requeues_with_backoff(tmp_path) -> None:
    q = _q(tmp_path)
    q.enqueue("e1", "L", {})
    msg = q.claim(now_ms=1000)  # attempts→1
    assert msg is not None
    q.fail(msg.claim_token, retryable=True, error="provider 429", now_ms=1000)
    assert q.stats().get("pending") == 1  # 回 pending 待重试(不是死信)
    assert q.claim(now_ms=1500) is None  # 退避中(next_visible_at=2000)暂不可领
    msg2 = q.claim(now_ms=2000)  # 到点可领
    assert msg2 is not None and msg2.attempts == 2  # 重试,attempts 递增


def test_failures_reach_dead_letter_at_max_attempts(tmp_path) -> None:
    q = _q(tmp_path, max_attempts=2)
    q.enqueue("e1", "L", {})
    m1 = q.claim(now_ms=1000)  # attempts→1
    q.fail(m1.claim_token, error="boom", now_ms=1000)  # 1<2 → 退避重投
    assert q.stats().get("pending") == 1
    m2 = q.claim(now_ms=10_000_000)  # 跳过退避,attempts→2
    q.fail(m2.claim_token, error="boom", now_ms=10_000_000)  # 2 不<2 → 死信
    assert q.stats().get("failed") == 1
    assert q.stats().get("pending", 0) == 0 and q.stats().get("claimed", 0) == 0


def test_non_retryable_goes_straight_to_dead_letter(tmp_path) -> None:
    q = _q(tmp_path)
    q.enqueue("e1", "L", {})
    m = q.claim(now_ms=1000)
    q.fail(m.claim_token, retryable=False, error="permanent", now_ms=1000)  # 不可重试 → 直接死信
    assert q.stats().get("failed") == 1
    assert q.stats().get("pending", 0) == 0


def test_requeue_dead_replays_to_pending(tmp_path) -> None:
    q = _q(tmp_path, max_attempts=1)
    q.enqueue("e1", "L", {})
    m = q.claim(now_ms=1000)
    q.fail(m.claim_token, error="boom", now_ms=1000)  # max_attempts=1 → 一次即死信
    assert q.stats().get("failed") == 1
    assert q.requeue_dead() == 1  # 重放
    assert q.stats().get("pending") == 1 and q.stats().get("failed", 0) == 0


def test_ensure_schema_adds_columns_to_legacy_table(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "legacy.db")
    with db.begin() as conn:  # 旧表:无 next_visible_at/last_error
        conn.execute(text(
            "CREATE TABLE ingress_messages (id INTEGER PRIMARY KEY, dedup_key TEXT UNIQUE, lane TEXT, "
            "payload TEXT, status TEXT, attempts INTEGER, claim_token TEXT, lease_until BIGINT, created_at BIGINT)"
        ))
        conn.execute(text(
            "INSERT INTO ingress_messages (dedup_key, lane, payload, status, attempts, created_at) "
            "VALUES ('d','L','{}','pending',0,1000)"
        ))
    q = IngressQueue(db, QueueConfig())
    q.ensure_schema()  # 幂等补列(#10 教训:create_all 不给已存在表加列)
    msg = q.claim(now_ms=2000)  # 新列默认值生效 → 老消息可正常领
    assert msg is not None and msg.lane == "L"
    q.ensure_schema()  # 再调一次仍幂等(列已存在不重复加)
