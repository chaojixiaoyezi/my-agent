"""E2：文件替换、容量、归还异常、嵌套作用域和真实 fork 的防护。"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from contextvars import copy_context

import pytest

from agent_py_agent.agent.io import sqlite_scope
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection
from agent_py_agent.agent.io.sqlite_scope import (
    scoped_connection,
    sqlite_connection_factory,
    sqlite_connection_scope,
)
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.support.sqlite_lease_assertions import (
    assert_reused_lease,
    physical_connection,
)


def _raw_lease(path):
    return scoped_connection(path, False, lambda: sqlite3.connect(path, factory=sqlite_connection_factory()))


def _raw_once(path):
    with _raw_lease(path) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    return conn


def _assert_closed(conn):
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        physical_connection(conn).execute("SELECT 1")


def test_copied_context_cannot_reopen_finished_scope(tmp_path, monkeypatch):
    opened = []
    connect = sqlite3.connect

    def observe(*args, **kwargs):
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", observe)
    path = tmp_path / "finished-scope.db"
    with sqlite_connection_scope(True):
        with _raw_lease(path) as conn:
            conn.execute("SELECT 1").fetchone()
        scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        copied = copy_context()
    assert scope.closed
    assert scope.entries == {}
    before = len(opened)
    for _ in range(2):
        copied.run(_raw_once, path)
    assert len(opened) - before == 2
    assert scope.entries == {}


def test_scope_exit_expires_lease_before_delayed_return(tmp_path):
    path = tmp_path / "delayed-return.db"
    with sqlite_connection_scope(True):
        manager = _raw_lease(path)
        conn = manager.__enter__()
        conn.execute("SELECT 1").fetchone()
    with pytest.raises(sqlite3.ProgrammingError) as caught:
        conn.execute("SELECT 1")
    assert caught.value.code == "SQLITE_LEASE_EXPIRED"
    manager.__exit__(None, None, None)


def test_inode_change_discards_old_connection_before_new_file_is_used(tmp_path):
    path = tmp_path / "store.db"
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as conn:
        conn.execute("CREATE TABLE restored(value TEXT)")
        conn.execute("INSERT INTO restored VALUES('new')")
    with sqlite_connection_scope(True):
        with _raw_lease(path) as old:
            old.execute("CREATE TABLE discarded(value TEXT)")
            old.commit()
        replacement.replace(path)
        with _raw_lease(path) as fresh:
            assert fresh is not old
            assert fresh.execute("SELECT value FROM restored").fetchone()[0] == "new"
            assert not fresh.execute("SELECT name FROM sqlite_master WHERE name='discarded'").fetchone()
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")


def test_missing_file_discards_connection_and_reconnects(tmp_path):
    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        with _raw_lease(path) as old:
            old.execute("CREATE TABLE disappeared(value TEXT)")
        path.replace(tmp_path / "removed.db")
        with _raw_lease(path) as fresh:
            assert fresh is not old
            assert path.exists()
            assert fresh.execute("SELECT name FROM sqlite_master WHERE name='disappeared'").fetchone() is None
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute("SELECT 1")


@pytest.mark.parametrize("store_type", [RuntimeRepository, LocalStore])
def test_real_wal_store_file_replacement_reads_restored_data(tmp_path, store_type):
    path = tmp_path / "store.db"
    replacement = tmp_path / "replacement.db"
    original, restored = store_type(path), store_type(replacement)
    def lease(store):
        return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()
    with lease(restored) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES('restored','yes')")
        conn.commit()
    with sqlite_connection_scope(True):
        with lease(original) as old:
            pass
        replacement.replace(path)
        with lease(original) as fresh:
            assert fresh is not old
            assert fresh.execute("SELECT value FROM metadata WHERE key='restored'").fetchone()[0] == "yes"


def test_capacity_is_bounded_overflow_connections_close_immediately(tmp_path):
    limit = sqlite_scope._SQLITE_SCOPE_MAX_CONNECTIONS_COUNT
    with sqlite_connection_scope(True):
        cached = [_raw_once(tmp_path / f"db-{index}.db") for index in range(limit)]
        for index, expected in enumerate(cached):
            assert_reused_lease(_raw_once(tmp_path / f"db-{index}.db"), expected)
        overflow = _raw_once(tmp_path / "overflow.db")
        _assert_closed(overflow)
    for conn in cached:
        _assert_closed(conn)


def test_nested_scopes_are_independent_and_restore_outer_scope(tmp_path):
    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        outer = _raw_once(path)
        with sqlite_connection_scope(True):
            inner = _raw_once(path)
            assert physical_connection(inner) is not physical_connection(outer)
        _assert_closed(inner)
        assert_reused_lease(_raw_once(path), outer)
        with sqlite_connection_scope(False):
            disabled = _raw_once(path)
            assert disabled is not outer
        _assert_closed(disabled)


def test_scope_exception_closes_every_cached_connection(tmp_path):
    connections = []
    with pytest.raises(ValueError), sqlite_connection_scope(True):
        connections = [_raw_once(tmp_path / f"store-{index}.db") for index in range(3)]
        raise ValueError("request failed")
    for conn in connections:
        _assert_closed(conn)


def test_return_failure_discards_and_closes_connection(tmp_path):
    closed = []

    class BrokenReturn(TrackedSQLiteConnection):
        @property
        def in_transaction(self):
            raise sqlite3.OperationalError("return failed")

        def close(self):
            closed.append(self)
            super().close()

    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        with scoped_connection(path, False, lambda: sqlite3.connect(path, factory=BrokenReturn)) as broken:
            pass
        assert closed == [physical_connection(broken)]
        with _raw_lease(path) as fresh:
            assert fresh is not broken
            assert fresh.execute("SELECT 1").fetchone()[0] == 1


def test_absolute_path_is_reused_across_lexical_aliases(tmp_path):
    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        with _raw_lease(path) as original:
            pass
        with _raw_lease(tmp_path / "." / "store.db") as normalized:
            assert_reused_lease(normalized, original)


def test_scoped_key_does_not_resolve_filesystem_paths(tmp_path, monkeypatch):
    from pathlib import Path

    def no_resolve(self, *args, **kwargs):
        raise AssertionError("lease must not call realpath")

    monkeypatch.setattr(Path, "resolve", no_resolve)
    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        first = _raw_once(path)
        assert_reused_lease(_raw_once(path), first)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), type("StopRequest", (BaseException,), {})()])
def test_rollback_interrupt_propagates_and_retires_connection(tmp_path, interrupt):
    class InterruptRollback(TrackedSQLiteConnection):
        def rollback(self):
            raise interrupt

    path = tmp_path / "store.db"
    with sqlite_connection_scope(True):
        with pytest.raises(type(interrupt)) as raised:
            _borrow_interrupted(path, InterruptRollback)
        assert raised.value is interrupt
        assert sqlite_scope._SQLITE_ACTIVE_SCOPE.get().entries == {}
        assert InterruptRollback.retired is not None
        _assert_closed(InterruptRollback.retired)
        assert _raw_once(path) is not InterruptRollback.retired


def _borrow_interrupted(path, factory):
    with scoped_connection(path, False, lambda: sqlite3.connect(path, factory=factory)) as conn:
        factory.retired = conn
        conn.execute("CREATE TABLE dirty(value TEXT)")
        conn.execute("INSERT INTO dirty VALUES('pending')")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="平台没有 fork；跨平台 PID 守卫由单测覆盖")
def test_real_fork_does_not_reuse_or_close_parent_connection(tmp_path):
    # 在单线程解释器中真正 fork，不在已运行 Gateway 替身线程的 pytest 进程里 fork。
    script = textwrap.dedent("""
        import os, sqlite3, sys
        from pathlib import Path
        from agent_py_agent.agent.io import sqlite_scope
        from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_factory, sqlite_connection_scope
        from agent_py_agent.tests.support.sqlite_lease_assertions import assert_reused_lease
        path = Path(sys.argv[1])
        def lease():
            return scoped_connection(path, False, lambda: sqlite3.connect(path, factory=sqlite_connection_factory()))
        with sqlite_connection_scope(True):
            with lease() as parent:
                pass
            pid = os.fork()
            if pid == 0:
                try:
                    with lease() as child:
                        assert child is not parent
                        assert child.execute('SELECT 1').fetchone()[0] == 1
                    try:
                        child.execute('SELECT 1')
                    except sqlite3.ProgrammingError:
                        pass
                    else:
                        raise AssertionError('child connection remained open')
                    sqlite_scope._close_scope(sqlite_scope._SQLITE_ACTIVE_SCOPE.get())
                    # 仅观察继承副本未关闭；业务入口不能解包租期层。
                    assert parent._target.execute('SELECT 1').fetchone()[0] == 1
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            with lease() as reused:
                assert_reused_lease(reused, parent)
                assert reused.execute('SELECT 1').fetchone()[0] == 1
        print('fork identities and parent lifetime verified')
    """)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path / "store.db")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "fork identities and parent lifetime verified"
