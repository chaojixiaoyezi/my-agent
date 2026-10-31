"""MC4-401：真实租期和游标逃逸不能污染下一借用者。"""

import sqlite3

import pytest

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeWriteFence


@pytest.fixture(params=[RuntimeRepository, LocalStore], ids=['runtime', 'local'])
def store(tmp_path, request):
    return request.param(tmp_path / 'store.db')


def lease(store):
    return store._runtime_connection() if isinstance(store, RuntimeRepository) else store._connection()


def assert_expired(operation):
    with pytest.raises(sqlite3.ProgrammingError) as caught:
        operation()
    assert getattr(caught.value, 'code', None) == 'SQLITE_LEASE_EXPIRED'


def assert_rejected(store, entry, operation):
    # RuntimeDB 写借用经 RuntimeWriteFence 无条件拒绝隐式提交脚本；其余入口仍须结构化报告租期过期。
    if isinstance(store, RuntimeRepository) and entry == 'executescript':
        with pytest.raises(ValueError, match='runtime executescript bypasses generation transaction fence'):
            operation()
        return
    assert_expired(operation)


def deferred_write(conn):
    conn.execute("INSERT INTO metadata VALUES('escaped', 'old')")
    yield 'unexpected write'


@pytest.mark.parametrize('escape', ['connection', 'callback', 'generator', 'bound-method'])
def test_old_lease_cannot_write_into_new_transaction(store, escape, monkeypatch):
    opened = []
    original = sqlite3.connect

    def observe(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', observe)
    with sqlite_connection_scope(True):
        with lease(store) as old:
            def callback():
                return old.execute("INSERT INTO metadata VALUES('escaped', 'old')")

            generator = deferred_write(old)
            saved = old.execute
        operations = {'connection': callback, 'callback': callback,
                      'generator': lambda: next(generator),
                      'bound-method': lambda: saved("INSERT INTO metadata VALUES('escaped', 'old')")}
        with lease(store) as new:
            assert new is not old
            new.execute("INSERT INTO metadata VALUES('fresh', 'new')")
            assert_expired(operations[escape])
            new.commit()
    assert len(opened) == 1  # 两个访问层仍只打开一条物理连接。
    with lease(store) as final:
        rows = final.execute("SELECT key, value FROM metadata WHERE key IN ('escaped','fresh')").fetchall()
        assert [tuple(row) for row in rows] == [('fresh', 'new')]


CONNECTION_OPERATIONS = {
    'execute': lambda conn: conn.execute('SELECT 1'),
    'executemany': lambda conn: conn.executemany('INSERT INTO metadata VALUES(?,?)', []),
    'executescript': lambda conn: conn.executescript('SELECT 1;'),
    'commit': lambda conn: conn.commit(),
    'rollback': lambda conn: conn.rollback(),
    'cursor': lambda conn: conn.cursor(),
    'close': lambda conn: conn.close(),
    'row_factory': lambda conn: conn.row_factory,
    'set_row_factory': lambda conn: setattr(conn, 'row_factory', None),
    'in_transaction': lambda conn: conn.in_transaction,
    'total_changes': lambda conn: conn.total_changes,
    'trace_callback': lambda conn: conn.set_trace_callback(None),
    'context_enter': lambda conn: conn.__enter__(),
    'context_exit': lambda conn: conn.__exit__(None, None, None),
}


@pytest.mark.parametrize('operation', CONNECTION_OPERATIONS)
def test_all_connection_entries_reject_expired_lease(store, operation):
    with sqlite_connection_scope(True):
        with lease(store) as old:
            old.execute('SELECT 1').fetchone()
        with lease(store) as new:
            assert_rejected(store, operation, lambda: CONNECTION_OPERATIONS[operation](old))
            assert new.execute('SELECT 2').fetchone()[0] == 2


CURSOR_OPERATIONS = {
    'execute': lambda cursor: cursor.execute('SELECT 1'),
    'executemany': lambda cursor: cursor.executemany('INSERT INTO metadata VALUES(?,?)', []),
    'executescript': lambda cursor: cursor.executescript('SELECT 1;'),
    'fetchone': lambda cursor: cursor.fetchone(),
    'fetchmany': lambda cursor: cursor.fetchmany(),
    'fetchall': lambda cursor: cursor.fetchall(),
    'iter': lambda cursor: iter(cursor),
    'next': lambda cursor: next(cursor),
    'close': lambda cursor: cursor.close(),
    'connection': lambda cursor: cursor.connection,
    'description': lambda cursor: cursor.description,
    'rowcount': lambda cursor: cursor.rowcount,
    'lastrowid': lambda cursor: cursor.lastrowid,
    'arraysize': lambda cursor: cursor.arraysize,
    'set_arraysize': lambda cursor: setattr(cursor, 'arraysize', 2),
    'row_factory': lambda cursor: cursor.row_factory,
}


@pytest.mark.parametrize('operation', CURSOR_OPERATIONS)
def test_all_cursor_entries_reject_expired_lease(store, operation):
    with sqlite_connection_scope(True):
        with lease(store) as conn:
            cursor = conn.execute('SELECT 1 UNION ALL SELECT 2')
            saved = cursor.fetchone
        with lease(store) as new:
            assert_rejected(store, operation, lambda: CURSOR_OPERATIONS[operation](cursor))
            assert_expired(saved)
            assert new.execute('SELECT 3').fetchone()[0] == 3


def test_live_public_interfaces_preserve_sqlite_semantics(store):
    with sqlite_connection_scope(True), lease(store) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.row_factory is sqlite3.Row
        if isinstance(store, RuntimeRepository):
            # RuntimeDB 借用拒绝隐式提交脚本；同一建表改走首写前核代际的 execute 入口。
            with pytest.raises(ValueError, match='runtime executescript bypasses generation transaction fence'):
                conn.executescript('CREATE TABLE lease_rows(value INTEGER);')
            conn.execute('CREATE TABLE lease_rows(value INTEGER)')
        else:
            conn.executescript('CREATE TABLE lease_rows(value INTEGER);')
        cursor = conn.cursor()
        assert cursor.connection is conn
        assert cursor.executemany('INSERT INTO lease_rows VALUES(?)', [(1,), (2,)]) is cursor
        assert cursor.rowcount == 2
        conn.commit()
        assert not conn.in_transaction
        assert cursor.execute('SELECT value FROM lease_rows ORDER BY value') is cursor
        cursor.arraysize = 1
        assert cursor.description[0][0] == 'value'
        assert cursor.fetchmany()[0]['value'] == 1
        assert next(iter(cursor))['value'] == 2
        assert cursor.fetchall() == []
        assert conn.total_changes == 2
        cursor.close()


def test_disabled_scope_exposes_unwrapped_standard_connection(store):
    with sqlite_connection_scope(False), lease(store) as conn:
        # RuntimeDB 写借用只外包代际栅栏；栅栏内仍是未包租期的标准连接。
        assert isinstance(conn, RuntimeWriteFence) is isinstance(store, RuntimeRepository)
        raw = conn._connection if isinstance(conn, RuntimeWriteFence) else conn
        assert type(raw) is sqlite3.Connection
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


@pytest.mark.parametrize('factory_target', ['connection', 'cursor'])
def test_row_factory_callback_does_not_receive_physical_connection(store, factory_target):
    captured = []

    def record_cursor(cursor, row):
        captured.append(cursor)
        return tuple(row)

    with sqlite_connection_scope(True):
        with lease(store) as old:
            target = old if factory_target == 'connection' else old.cursor()
            target.row_factory = record_cursor
            cursor = old.cursor() if factory_target == 'connection' else target
            assert target.row_factory is record_cursor
            assert cursor.execute('SELECT 1').fetchone() == (1,)
            escaped = captured[0]
            assert escaped.connection is old
        with lease(store) as new:
            new.execute("INSERT INTO metadata VALUES('fresh', 'new')")
            assert_expired(lambda: escaped.connection.execute("INSERT INTO metadata VALUES('escaped', 'old')"))
            assert_expired(lambda: escaped.execute('SELECT 1'))
            new.commit()
    with lease(store) as final:
        assert final.execute("SELECT key FROM metadata WHERE key IN ('escaped','fresh')").fetchall()[0][0] == 'fresh'
