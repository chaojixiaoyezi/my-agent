"""MC4-05：未结束语句不能跨借用占用 WAL 读锁。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeWriteFence
from agent_py_agent.tests.support import sqlite_lease_assertions


# RuntimeDB 写借用外包 RuntimeWriteFence；租期与物理连接身份观察取栅栏内层借用。
def _lease_layer(conn):
    return conn._connection if isinstance(conn, RuntimeWriteFence) else conn


def assert_reused_lease(current, previous):
    sqlite_lease_assertions.assert_reused_lease(_lease_layer(current), _lease_layer(previous))


@pytest.fixture(params=[RuntimeRepository, LocalStore], ids=["runtime", "local"])
def store(tmp_path, request):
    store = request.param(tmp_path / "store.db")
    with lease(store) as conn:
        conn.execute("CREATE TABLE checkpoint_rows(value INTEGER)")
        conn.executemany("INSERT INTO checkpoint_rows VALUES(?)", [(n,) for n in range(200)])
        conn.commit()
    return store


def lease(store):
    return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()


def _write_and_checkpoint(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("INSERT INTO checkpoint_rows VALUES(999)")
        conn.commit()
        return tuple(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    finally:
        conn.close()


def _half_read(store, api):
    with lease(store) as conn:
        if api == "cursor":
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM checkpoint_rows")
        else:
            cursor = conn.execute("SELECT value FROM checkpoint_rows")
        assert cursor.fetchone()[0] == 0
        assert conn.in_transaction is False
    return conn, cursor


@pytest.mark.parametrize("api", ["execute", "cursor"])
def test_half_read_cursor_is_closed_and_checkpoint_completes(store, api):
    with sqlite_connection_scope(True):
        original, cursor = _half_read(store, api)
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_write_and_checkpoint, store.db_path).result() == (0, 0, 0)
        with lease(store) as reused:
            assert_reused_lease(reused, original)
            assert reused.execute("SELECT count(*) FROM checkpoint_rows").fetchone()[0] == 201


def test_half_read_exception_also_releases_wal_lock(store):
    with sqlite_connection_scope(True):
        with pytest.raises(ValueError), lease(store) as conn:
            cursor = conn.execute("SELECT value FROM checkpoint_rows")
            assert cursor.fetchone()[0] == 0
            raise ValueError("body failed before fetchall")
        with pytest.raises(sqlite3.ProgrammingError):
            cursor.fetchone()
        assert _write_and_checkpoint(store.db_path) == (0, 0, 0)


def test_untracked_connection_is_retired_instead_of_cached(tmp_path):
    path = tmp_path / "raw.db"
    with sqlite_connection_scope(True):
        with scoped_connection(path, False, lambda: sqlite3.connect(path)) as conn:
            conn.execute("SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_foreign_reader_prevents_clean_wal_return_so_writer_is_retired(store):
    reader = sqlite3.connect(store.db_path)
    cursor = reader.execute("SELECT value FROM checkpoint_rows")
    assert cursor.fetchone()[0] == 0
    try:
        with sqlite_connection_scope(True):
            writer = _commit_checkpoint_row(store, 222)
            _assert_retired(writer)
    finally:
        cursor.close()
        reader.close()
    with lease(store) as conn:
        assert conn.execute("SELECT count(*) FROM checkpoint_rows WHERE value=222").fetchone()[0] == 1


def test_read_only_reuse_does_not_repeat_checkpoint(store):
    statements = []
    with sqlite_connection_scope(True):
        with lease(store) as conn:
            conn.execute("SELECT 1").fetchone()
        # 首次 PRAGMA/WAL 收口不属于重复纯读；在下一次有效借用中安装观察，不能操作已归还访问层。
        with lease(store) as observer:
            assert_reused_lease(observer, conn)
            observer.set_trace_callback(statements.append)
        for _ in range(5):
            _read_same_connection(store, conn)
    assert not any("WAL_CHECKPOINT" in sql.upper() for sql in statements)


def _assert_retired(conn):
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def _commit_checkpoint_row(store, value):
    with lease(store) as writer:
        writer.execute("INSERT INTO checkpoint_rows VALUES(?)", (value,))
        writer.commit()
    return writer


def _read_same_connection(store, conn):
    with lease(store) as reused:
        assert_reused_lease(reused, conn)
        assert reused.execute("SELECT 1").fetchone()[0] == 1
