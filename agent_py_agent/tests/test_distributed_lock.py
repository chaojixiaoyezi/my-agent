"""Tier 0.3 分布式锁测试:PG advisory lock 跨连接互斥(真测)+ SQLite 本地回退。"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")

from agent_py_agent.agent.distributed_lock import DistributedLock, key_to_lock_id  # noqa: E402
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
        pytest.skip(f"无可用 PostgreSQL 实例做真测: {type(exc).__name__}")


def test_key_to_lock_id_deterministic_int64() -> None:
    a = key_to_lock_id("gateway:task:123")
    assert a == key_to_lock_id("gateway:task:123")  # 确定性
    assert -(2**63) <= a < 2**63  # 落在有符号 bigint 范围
    assert key_to_lock_id("other") != a


def test_sqlite_local_fallback_mutual_exclusion() -> None:
    lock = DistributedLock(StorageBackend.in_memory())
    with lock.try_acquire("k") as got1:
        assert got1 is True
        with lock.try_acquire("k") as got2:  # 同实例同键已持锁 → 非阻塞拿不到
            assert got2 is False
    # 释放后能再拿
    with lock.try_acquire("k") as got3:
        assert got3 is True


def test_postgres_advisory_lock_cross_connection_exclusion_real() -> None:
    """真 PG:一个连接持 advisory lock 时,另一连接 try 拿同键必失败(跨实例互斥铁证)。"""
    db = _pg_backend()
    lock = DistributedLock(db)
    key = "scale-test-lock-key-xyz"
    try:
        with lock.try_acquire(key) as got1:  # 连接1 持锁
            assert got1 is True
            with lock.try_acquire(key) as got2:  # 连接2 拿同键 → 失败(被连接1 独占)
                assert got2 is False
        # 连接1 释放后,再拿成功
        with lock.try_acquire(key) as got3:
            assert got3 is True
    finally:
        db.dispose()


def test_postgres_different_keys_dont_block_real() -> None:
    db = _pg_backend()
    lock = DistributedLock(db)
    try:
        with lock.try_acquire("key-A") as a, lock.try_acquire("key-B") as b:
            assert a is True and b is True  # 不同键互不阻塞
    finally:
        db.dispose()
