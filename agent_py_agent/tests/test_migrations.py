"""审计 #10 修复真测:版本化迁移在"已有数据的老库"上安全加列、幂等、失败整批回滚。

真起 SQLite(+ 可用时真 PostgreSQL):模拟已上线客户库,迁移加列不丢老数据、重复 apply no-op、
某步失败整事务回滚(连前面成功的也不记版本/不留半成品 DDL)。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
from sqlalchemy import text  # noqa: E402

from agent_py_agent.agent.migrations import Migration, MigrationError, MigrationRunner  # noqa: E402
from agent_py_agent.agent.storage_backend import StorageBackend  # noqa: E402


def _add_tenant(conn) -> None:
    conn.execute(text("ALTER TABLE items ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'"))


def test_migration_adds_column_to_existing_db_without_data_loss(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "m.db")
    with db.begin() as conn:  # 老库:已有表 + 数据(模拟已上线客户)
        conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO items (id, name) VALUES (1, 'alice')"))
    runner = MigrationRunner(db, [Migration(1, "add_tenant", _add_tenant)])
    assert len(runner.apply_pending()) == 1
    with db.connect() as conn:
        rows = conn.execute(text("SELECT id, name, tenant FROM items")).fetchall()
    assert [tuple(r) for r in rows] == [(1, "alice", "default")]  # 老数据保留 + 新列有默认值
    assert runner.apply_pending() == []  # 幂等:再 apply → no-op
    assert runner.applied_versions() == [1]


def test_failed_migration_rolls_back_whole_batch(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "m.db")
    with db.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))

    def good(conn) -> None:
        conn.execute(text("ALTER TABLE t ADD COLUMN a TEXT"))

    def bad(conn) -> None:
        conn.execute(text("ALTER TABLE t ADD COLUMN a TEXT"))  # 重复加列 → 报错

    runner = MigrationRunner(db, [Migration(1, "good", good), Migration(2, "bad", bad)])
    with pytest.raises(MigrationError):
        runner.apply_pending()
    assert runner.applied_versions() == []  # 单事务:连版本 1 也没记录
    with db.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(t)")).fetchall()]
    assert "a" not in cols  # 列 a 也被回滚,无半成品 DDL


def test_duplicate_version_rejected(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "m.db")
    with pytest.raises(MigrationError):
        MigrationRunner(db, [Migration(1, "x", _add_tenant), Migration(1, "y", _add_tenant)])


def test_pending_read_only_does_not_create_version_table(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "m.db")
    runner = MigrationRunner(db, [Migration(1, "x", lambda _conn: None)])
    assert [item.version for item in runner.pending_read_only()] == [1]
    assert "schema_migrations" not in db.table_names()


def test_require_current_fails_until_migrations_are_applied(tmp_path) -> None:
    db = StorageBackend.for_path(tmp_path / "m.db")
    runner = MigrationRunner(db, [Migration(1, "x", lambda _conn: None)])
    with pytest.raises(MigrationError, match="schema 未升级"):
        runner.require_current()
    runner.apply_pending()
    runner.require_current()


def test_migration_on_real_postgres() -> None:
    url = os.environ.get("TEST_POSTGRES_URL", "postgresql+psycopg://localhost:5432/postgres")
    try:
        db = StorageBackend(url)
        with db.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"无可用 PostgreSQL 做真测: {type(exc).__name__}")
    tbl = "test_mig_items_tier0"
    with db.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
        conn.execute(text(f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text(f"INSERT INTO {tbl} (id, name) VALUES (1, 'bob')"))

    def add_col(conn) -> None:
        conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'"))

    try:
        runner = MigrationRunner(db, [Migration(1, "add_tenant", add_col)])
        assert len(runner.apply_pending()) == 1  # PG 路径走 pg_advisory_xact_lock
        with db.connect() as conn:
            row = conn.execute(text(f"SELECT name, tenant FROM {tbl} WHERE id=1")).fetchone()
        assert tuple(row) == ("bob", "default")  # 老数据保留 + 默认值
        assert runner.apply_pending() == []  # 幂等
    finally:
        with db.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
            conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))
        db.dispose()
