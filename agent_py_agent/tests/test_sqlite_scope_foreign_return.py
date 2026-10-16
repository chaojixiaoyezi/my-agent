"""复制Context后在另一线程结束借用，也不能操作创建线程的物理连接。"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import copy_context

import pytest

from agent_py_agent.agent.io import sqlite_scope
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection
from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def observed_factory(foreign):
    owner = threading.current_thread()

    class ObservedConnection(TrackedSQLiteConnection):
        def observe(self, operation):
            if threading.current_thread() is not owner:
                foreign.append(operation)

        @property
        def in_transaction(self):
            self.observe('in_transaction')
            return super().in_transaction

        def rollback(self):
            self.observe('rollback')
            return super().rollback()

        def close_lease_cursors(self):
            self.observe('close_lease_cursors')
            return super().close_lease_cursors()

        def close(self):
            self.observe('close')
            return super().close()

    return ObservedConnection


@pytest.mark.parametrize('mode', ['cached', 'nested', 'overflow'])
def test_foreign_thread_return_never_operates_creator_connection(tmp_path, monkeypatch, mode):
    foreign = []
    factory = observed_factory(foreign)
    path = tmp_path / 'thread.db'
    if mode == 'overflow':
        monkeypatch.setattr(sqlite_scope, '_SQLITE_SCOPE_MAX_CONNECTIONS_COUNT', 0)
    def borrow():
        return scoped_connection(path, False, lambda: sqlite3.connect(path, factory=factory))

    with ExitStack() as contexts:
        contexts.enter_context(sqlite_connection_scope(True))
        if mode == 'nested':
            contexts.enter_context(borrow())
        manager = borrow()
        conn = contexts.enter_context(manager)
        conn.execute('CREATE TABLE facts(value TEXT)')
        conn.execute("INSERT INTO facts VALUES('pending')")
        conn.row_factory = lambda cursor, row: tuple(row)
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, manager.__exit__, None, None, None).result(timeout=5)
        assert foreign == []
        with pytest.raises(sqlite3.ProgrammingError) as caught:
            conn.execute('SELECT 1')
        assert caught.value.code == 'SQLITE_LEASE_EXPIRED'
        assert conn._target.in_transaction
        conn._target.row_factory = None  # 创建线程仅用物理观测核实未提交数据没有被异地回滚。
        assert conn._target.execute('SELECT value FROM facts').fetchone()[0] == 'pending'


def store_borrow(store):
    return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()


def observe_creator_only(original, operation, foreign):
    creator = threading.current_thread()

    def observed(conn):
        if threading.current_thread() is not creator:
            foreign.append(operation)
        return original(conn)

    return observed


def assert_expired(lease):
    with pytest.raises(sqlite3.ProgrammingError) as caught:
        lease.execute('SELECT 1')
    assert caught.value.code == 'SQLITE_LEASE_EXPIRED'


def independent_write(path):
    try:
        with sqlite3.connect(path, timeout=0) as conn:
            conn.execute("INSERT INTO facts VALUES('fresh')")
            conn.commit()
            return None, conn.execute('SELECT value FROM facts').fetchall()
    except sqlite3.OperationalError as exc:
        return str(exc), None


def fill_real_stores(fillers):
    for filler in fillers:
        with store_borrow(filler) as lease:
            lease.execute('SELECT 1').fetchone()


def foreign_return(store, mode, fillers):
    with ExitStack() as active:
        if mode == 'nested':
            active.enter_context(store_borrow(store))
        fill_real_stores(fillers)
        scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        if mode == 'overflow':
            assert len(scope.entries) == 8
        manager = store_borrow(store)
        lease = manager.__enter__()
        cached = any(entry.conn is lease._target for entry in scope.entries.values())
        assert cached is (mode == 'cached')
        lease.execute("INSERT INTO facts VALUES('pending')")
        lease.row_factory = lambda cursor, row: tuple(row)
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, manager.__exit__, None, None, None).result(timeout=5)
        assert_expired(lease)
        assert lease._target.in_transaction
        return lease


@pytest.mark.parametrize('store_type', [RuntimeRepository, LocalStore])
@pytest.mark.parametrize('mode', ['cached', 'nested', 'overflow'])
def test_real_factory_foreign_return_releases_wal_writer_at_creator_scope_exit(tmp_path, monkeypatch, store_type, mode):
    foreign = []
    for operation in ('close', 'rollback', 'close_lease_cursors', '_checkpoint_idle_writes', '_truncate_idle_wal'):
        original = getattr(TrackedSQLiteConnection, operation)
        monkeypatch.setattr(TrackedSQLiteConnection, operation, observe_creator_only(original, operation, foreign))
    path = tmp_path / 'real-wal.db'
    store = store_type(path)
    with store_borrow(store) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        conn.execute('CREATE TABLE facts(value TEXT)')
        conn.commit()
    fillers = [store_type(tmp_path / f'fill-{index}.db') for index in range(8)] if mode == 'overflow' else []
    with sqlite_connection_scope(True):
        old_lease = foreign_return(store, mode, fillers)
        assert foreign == []
    try:
        error, rows = independent_write(path)
        assert error is None, error
        assert rows == [('fresh',)]
        assert_expired(old_lease)
        assert foreign == []
    finally:
        old_lease._target.close()  # 红测也回收本测试保留的物理引用，不靠GC解除写锁。
