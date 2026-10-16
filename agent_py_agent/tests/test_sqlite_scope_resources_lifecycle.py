"""MC4-06/12：复用与短连接的文件生命周期一致，资源开关数平衡。"""

from __future__ import annotations

import sqlite3
import sys

import pytest

from agent_py_agent.agent.io import sqlite_scope
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection
from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def lease(store):
    return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()


def _set_value(store, value):
    with lease(store) as conn:
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('lifecycle',?)", (value,))
        conn.commit()


def _get_value(store):
    with lease(store) as conn:
        return conn.execute("SELECT value FROM metadata WHERE key='lifecycle'").fetchone()[0]


def _change_path(store, scenario):
    path = store.db_path
    trash = path.with_name("trashed.db")
    if scenario == "trash":
        path.replace(trash)
        type(store)(path)
        return trash
    replacement = path.with_name("replacement.db")
    restored = type(store)(replacement)
    _set_value(restored, "restored")
    if scenario == "recreate":
        path.replace(trash)
    replacement.replace(path)
    return trash if scenario == "recreate" else None


def _lifecycle_round(root, store_type, scenario, enabled):
    store = store_type(root / "store.db")
    _set_value(store, "original")
    with sqlite_connection_scope(enabled):
        old, trash = _borrow_then_change_path(store, scenario)
        _set_value(store, "after-change")
        current_value = _get_value(store)
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")
    trash_value = _get_value(store_type(trash)) if trash is not None else None
    return current_value, trash_value


def _borrow_then_change_path(store, scenario):
    if scenario == "borrowed":
        with lease(store) as old:
            old.execute("SELECT 1").fetchone()
            trash = _change_path(store, scenario)
        return old, trash
    with lease(store) as old:
        old.execute("SELECT 1").fetchone()
    return old, _change_path(store, scenario)


@pytest.mark.parametrize("store_type", [RuntimeRepository, LocalStore], ids=["runtime", "local"])
@pytest.mark.parametrize("scenario", ["idle", "borrowed", "trash", "recreate"])
def test_file_lifecycle_matches_short_connections(tmp_path, store_type, scenario):
    short = _lifecycle_round(tmp_path / "short", store_type, scenario, False)
    reused = _lifecycle_round(tmp_path / "reuse", store_type, scenario, True)
    assert reused == short
    assert reused[0] == "after-change"
    if scenario in {"trash", "recreate"}:
        assert reused[1] == "original"  # 后续写入不得落进回收站的旧 inode


@pytest.mark.parametrize("raise_error", [False, True])
def test_cached_nested_and_overflow_open_close_counts_balance(tmp_path, raise_error):
    opened, closed, audit_events = [], [], []
    active = [True]

    def audit(event, args):
        if active[0] and event == "sqlite3.connect":
            audit_events.append(args[0])

    class CountedConnection(TrackedSQLiteConnection):
        def close(self):
            closed.append(self)
            super().close()

    def connect(path):
        conn = sqlite3.connect(path, factory=CountedConnection)
        opened.append(conn)
        return conn

    def borrow(path):
        return scoped_connection(path, False, lambda: connect(path))

    sys.addaudithook(audit)
    try:
        _resource_round(tmp_path, borrow, raise_error)
        assert len(audit_events) == len(opened) == len(closed)
        assert {id(conn) for conn in opened} == {id(conn) for conn in closed}
        assert len({id(conn) for conn in closed}) == len(closed)
    finally:
        active[0] = False


def _resource_round(root, borrow, raise_error):
    try:
        with sqlite_connection_scope(True):
            _resource_scope_body(root, borrow, raise_error)
    except ValueError:
        assert raise_error


def _resource_scope_body(root, borrow, raise_error):
    paths = [root / f"{n}.db" for n in range(sqlite_scope._SQLITE_SCOPE_MAX_CONNECTIONS_COUNT)]
    for path in paths:
        with borrow(path) as conn:
            conn.execute("SELECT 1").fetchone()
    _nested_borrows(borrow, paths[0])
    with borrow(root / "overflow.db") as conn:
        conn.execute("SELECT 1").fetchone()
    if raise_error:
        raise ValueError("request interrupted")


def _nested_borrows(borrow, path):
    with borrow(path) as outer, borrow(path) as inner, borrow(path) as deeper:
        assert len({id(outer), id(inner), id(deeper)}) == 3


def test_cursor_ddl_survives_database_only_rename_after_return(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "destination.db"
    with sqlite_connection_scope(True):
        with scoped_connection(source, False, lambda: sqlite3.connect(source, factory=TrackedSQLiteConnection)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.cursor().execute("CREATE TABLE ddl_only(value TEXT)")
        source.replace(destination)
        with scoped_connection(destination, False, lambda: sqlite3.connect(destination, factory=TrackedSQLiteConnection)) as fresh:
            assert fresh.execute("SELECT 1 FROM sqlite_master WHERE name='ddl_only'").fetchone()
