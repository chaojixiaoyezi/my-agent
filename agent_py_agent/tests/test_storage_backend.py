"""Tier 0.1 存储抽象测试:SQLite 真测 + PostgreSQL 真测 + 同代码两端可移植性。

真实测试(用户要求):若本机有可连的 PostgreSQL(env TEST_POSTGRES_URL 或默认 localhost:5432/postgres),
跑真 PG;否则 skip(不假测)。可移植性测试用同一套 SQLAlchemy Core 代码在两端各跑一遍、断言结果一致。
"""

from __future__ import annotations

import os

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")  # scale extra 未装则整文件 skip
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select, text  # noqa: E402

from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def _crud_roundtrip(db: StorageBackend) -> list[tuple[int, str]]:
    """同一套 Core 代码——SQLite/PostgreSQL 都能跑(可移植性的核心证明)。"""
    meta = MetaData()
    t = Table("scale_portability_test", meta, Column("id", Integer, primary_key=True), Column("name", String(50)))
    meta.create_all(db.engine)
    try:
        with db.begin() as conn:  # 写事务(SQLite→BEGIN IMMEDIATE)
            conn.execute(insert(t), [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}])
        with db.connect() as conn:  # 只读
            rows = conn.execute(select(t).order_by(t.c.id)).fetchall()
        return [(int(r.id), str(r.name)) for r in rows]
    finally:
        meta.drop_all(db.engine)
        db.dispose()


def _pg_backend() -> StorageBackend:
    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url)
        with db.connect() as conn:
            conn.execute(text("SELECT 1"))
        return db
    except Exception as exc:  # 无可用 PG → 跳过真测(不假装)
        pytest.skip(f"无可用 PostgreSQL 实例做真测: {type(exc).__name__}")


# --- SQLite 真测 ---
def test_sqlite_in_memory_crud() -> None:
    assert _crud_roundtrip(StorageBackend.in_memory()) == [(1, "alice"), (2, "bob")]


def test_sqlite_file_sets_wal_and_dialect(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "store.db")
    try:
        assert db.is_sqlite is True and db.is_postgres is False
        assert db.dialect == "sqlite"
        assert str(db.scalar("PRAGMA journal_mode")).lower() == "wal"  # WAL 真启用
    finally:
        db.dispose()


def test_begin_is_write_transaction_and_persists(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "w.db")
    result = _crud_roundtrip(db)
    assert result == [(1, "alice"), (2, "bob")]


# --- PostgreSQL 真测(有则真跑,无则 skip)---
def test_postgres_dialect_and_crud_real() -> None:
    db = _pg_backend()
    assert db.is_postgres is True
    assert _crud_roundtrip(db) == [(1, "alice"), (2, "bob")]


def test_portability_same_code_both_backends() -> None:
    """可移植性铁证:同一 _crud_roundtrip 代码,SQLite 与真 PostgreSQL 结果完全一致。"""
    sqlite_result = _crud_roundtrip(StorageBackend.in_memory())
    pg_result = _crud_roundtrip(_pg_backend())  # 无 PG 会在此 skip
    assert sqlite_result == pg_result == [(1, "alice"), (2, "bob")]


def test_postgres_pool_is_tuned_real() -> None:
    """PG 连接池按参数调优(研究发现 参考实现 零调优默认仅 5;10 万并发必须可调)。"""
    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url, pool_size=7, max_overflow=12)
        with db.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"无可用 PostgreSQL: {type(exc).__name__}")
    try:
        assert db.engine.pool.size() == 7  # 池大小生效(非 SQLAlchemy 默认 5)
    finally:
        db.dispose()
