"""E2：真实 SQLite 连接与审计事件覆盖事务、身份及资源寿命。"""

from __future__ import annotations

import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeWriteFence
from agent_py_agent.tests.support import sqlite_lease_assertions


# RuntimeDB 写借用外包 RuntimeWriteFence；租期与物理连接身份观察取栅栏内层借用。
def _lease_layer(conn):
    return conn._connection if isinstance(conn, RuntimeWriteFence) else conn


def physical_connection(conn):
    return sqlite_lease_assertions.physical_connection(_lease_layer(conn))


def assert_reused_lease(current, previous):
    sqlite_lease_assertions.assert_reused_lease(_lease_layer(current), _lease_layer(previous))


@pytest.fixture(params=[RuntimeRepository, LocalStore], ids=["runtime", "local"])
def store(tmp_path, request):
    return request.param(tmp_path / "store.db")


def lease(store):
    return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()


def _read_once(store):
    with lease(store) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    return conn


def _save_metadata(store, key, value):
    with lease(store) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (key, value))
        conn.commit()
    return conn


def _read_metadata(store, key):
    with lease(store) as conn:
        return conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]


def _nested_transaction(store):
    with lease(store) as outer:
        outer.execute("INSERT INTO metadata(key,value) VALUES('uncommitted','outer')")
        with lease(store) as inner:
            assert physical_connection(inner) is not physical_connection(outer)
            assert inner.execute("SELECT value FROM metadata WHERE key='uncommitted'").fetchone() is None
            inner.commit()
        assert outer.in_transaction
    return outer


def _dirty_lease(store, raise_error):
    with lease(store) as dirty:
        dirty.execute("INSERT INTO metadata(key,value) VALUES('discard','x')")
        if raise_error:
            raise ValueError("body failed")


def _assert_read_only(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO metadata(key,value) VALUES('forbidden','x')")


def _inherited_connections_short(store, count=2):
    connections = []
    for _ in range(count):
        conn = _read_once(store)
        connections.append(conn)
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert connections[0] is not connections[1]


def _read_pragmas(store):
    with lease(store) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


@pytest.fixture
def connect_events():
    events = []
    active = [True]

    def audit(event, args):
        if active[0] and event == "sqlite3.connect":
            events.append(args[0])

    sys.addaudithook(audit)
    yield events
    active[0] = False


@pytest.mark.parametrize("enabled,expected", [(False, 5), (True, 1)])
def test_scope_switch_controls_real_connect_count(store, connect_events, enabled, expected):
    with sqlite_connection_scope(enabled):
        connections = [_read_once(store) for _ in range(5)]
        conn = connections[-1]
    assert len(connect_events) == expected
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_outside_scope_remains_short_lived(store, connect_events):
    # 明确声明没有请求作用域，使外层启用回归也保留原短连接观察点，不跳过或放宽计数断言。
    with sqlite_connection_scope(False):
        _inherited_connections_short(store, 3)
    assert len(connect_events) == 3


def test_nested_leases_never_share_or_commit_outer_transaction(store, connect_events):
    with sqlite_connection_scope(True):
        outer = _nested_transaction(store)
        with lease(store) as clean:
            assert_reused_lease(clean, outer)
            assert clean.execute("SELECT value FROM metadata WHERE key='uncommitted'").fetchone() is None
    assert len(connect_events) == 2


@pytest.mark.parametrize("raise_error", [False, True])
def test_return_rolls_back_uncommitted_changes_before_reuse(store, raise_error):
    with sqlite_connection_scope(True):
        dirty = _read_once(store)
        try:
            _dirty_lease(store, raise_error)
        except ValueError:
            assert raise_error
        with lease(store) as clean:
            assert_reused_lease(clean, dirty)
            assert clean.in_transaction is False
            assert clean.execute("SELECT value FROM metadata WHERE key='discard'").fetchone() is None


def test_committed_changes_survive_reuse_and_scope_exit(store):
    with sqlite_connection_scope(True):
        with lease(store) as conn:
            conn.execute("INSERT INTO metadata(key,value) VALUES('committed','yes')")
            conn.commit()
        with lease(store) as reused:
            assert_reused_lease(reused, conn)
            assert reused.execute("SELECT value FROM metadata WHERE key='committed'").fetchone()[0] == "yes"
    with lease(store) as fresh:
        assert fresh.execute("SELECT value FROM metadata WHERE key='committed'").fetchone()[0] == "yes"


def test_owners_with_same_filename_have_independent_connections(tmp_path, connect_events):
    owners = [RuntimeRepository(tmp_path / owner / "runtime.db") for owner in ("a", "b")]
    connect_events.clear()
    with sqlite_connection_scope(True):
        for owner, repo in zip(("a", "b"), owners, strict=True):
            _save_metadata(repo, "owner", owner)
        for owner, repo in zip(("a", "b"), owners, strict=True):
            assert _read_metadata(repo, "owner") == owner
    assert len(connect_events) == 2


def test_read_only_and_read_write_are_never_interchangeable(tmp_path, connect_events):
    path = tmp_path / "runtime.db"
    writer = RuntimeRepository(path)
    reader = RuntimeRepository(path, read_only=True)
    connect_events.clear()
    with sqlite_connection_scope(True):
        with writer._runtime_connection() as rw:
            pass
        with reader._runtime_connection() as ro:
            assert physical_connection(rw) is not physical_connection(ro)
            _assert_read_only(ro)
        with writer._runtime_connection() as write_again:
            assert_reused_lease(write_again, rw)
    assert len(connect_events) == 2


def test_inherited_context_in_other_thread_uses_only_short_connections(store):
    with sqlite_connection_scope(True):
        with lease(store) as parent:
            pass
        context = copy_context()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, _inherited_connections_short, store).result()
        with lease(store) as reused:
            assert_reused_lease(reused, parent)


def test_two_threads_own_distinct_scopes_and_persist_writes(store):
    # 写后归还要对 WAL 做收口（PASSIVE 回填 + 零忙等 TRUNCATE）；另一连接同时使用同一库时
    # 收口会失败，产品按设计退役该连接（失败退役是刻意的保守策略，只损失复用、不影响正确性）。
    # 复用断言只有在无竞争的借用段才确定，所以让两次借用与归还串行；两个线程的 scope
    # 依然各自独立、同时存在，distinct 断言不受影响。
    serial = threading.Lock()

    def borrow_and_reuse(value):
        with serial:
            with lease(store) as conn:
                conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (value, value))
                conn.commit()
            with lease(store) as reused:
                assert_reused_lease(reused, conn)
        return conn

    def worker(value):
        with sqlite_connection_scope(True):
            return borrow_and_reuse(value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        connections = list(pool.map(worker, ("a", "b")))
    assert physical_connection(connections[0]) is not physical_connection(connections[1])
    with lease(store) as conn:
        assert conn.execute("SELECT count(*) FROM metadata WHERE key IN ('a','b')").fetchone()[0] == 2


def _owner_thread_operations(repo):
    owner = repo.db_path.parent.name
    with sqlite_connection_scope(True):
        original = _save_metadata(repo, "owner", owner)
        assert_reused_lease(_read_once(repo), original)
        assert _read_metadata(repo, "owner") == owner
    return original


def test_concurrent_owners_keep_same_filename_and_values_isolated(tmp_path):
    owners = [RuntimeRepository(tmp_path / owner / "runtime.db") for owner in ("alice", "bob")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        connections = list(pool.map(_owner_thread_operations, owners))
    assert physical_connection(connections[0]) is not physical_connection(connections[1])
    assert [_read_metadata(repo, "owner") for repo in owners] == ["alice", "bob"]


def test_pragmas_set_only_on_new_connection(store, monkeypatch):
    statements = []
    original = sqlite3.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    with sqlite_connection_scope(True):
        for _ in range(4):
            _read_pragmas(store)
    assert sum(sql == "PRAGMA busy_timeout=30000" for sql in statements) == 1
    assert sum(sql == "PRAGMA journal_mode=WAL" for sql in statements) == 1
    assert sum(sql == "PRAGMA foreign_keys=ON" for sql in statements) == 1


def test_runtime_transaction_stays_independent_inside_scope(tmp_path, connect_events):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    connect_events.clear()
    with sqlite_connection_scope(True):
        with repo._runtime_connection() as cached:
            pass
        with repo.transaction() as explicit:
            assert physical_connection(explicit) is not physical_connection(cached)
            explicit.execute("INSERT INTO metadata(key,value) VALUES('tx','committed')")
        with pytest.raises(sqlite3.ProgrammingError):
            explicit.execute("SELECT 1")
        with pytest.raises(ValueError), repo.transaction() as failed:
            failed.execute("INSERT INTO metadata(key,value) VALUES('failed','discard')")
            raise ValueError("rollback")
        with repo._runtime_connection() as reused:
            assert_reused_lease(reused, cached)
            assert reused.execute("SELECT value FROM metadata WHERE key='tx'").fetchone()[0] == "committed"
            assert reused.execute("SELECT value FROM metadata WHERE key='failed'").fetchone() is None
    assert len(connect_events) == 3
