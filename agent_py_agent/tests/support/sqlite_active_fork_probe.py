"""单线程解释器里的真 fork：观察继承借用的归还，不连接真实 Gateway。"""

import json
import os
import sqlite3
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection
from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope

ORIGIN_PID = os.getpid()
FOREIGN_CALLS = []
OPENED = []  # 子进程持有继承副本到 os._exit，避免把 GC 析构当成显式清理。


def foreign_call(name):
    if os.getpid() == ORIGIN_PID:
        return False
    FOREIGN_CALLS.append(name)
    return True


class ObservedConnection(TrackedSQLiteConnection):
    @property
    def in_transaction(self):
        foreign_call('in_transaction')
        return super().in_transaction

    def rollback(self):
        if not foreign_call('rollback'):
            super().rollback()

    def close_lease_cursors(self):
        if not foreign_call('close_lease_cursors'):
            super().close_lease_cursors()

    def _checkpoint_idle_writes(self):
        if not foreign_call('checkpoint'):
            super()._checkpoint_idle_writes()

    def close(self):
        if not foreign_call('close'):
            super().close()


def connect(path):
    factory = ObservedConnection if os.getpid() == ORIGIN_PID else sqlite3.Connection
    conn = sqlite3.connect(path, timeout=1, factory=factory)
    conn.execute('PRAGMA journal_mode=WAL')
    OPENED.append(conn)
    return conn


def borrow(path):
    return scoped_connection(path, False, lambda: connect(path))


def fill_cache(path):
    for index in range(8):
        with borrow(path.with_name(f'cache-{index}.db')) as conn:
            conn.execute('SELECT 1').fetchone()


@contextmanager
def active_borrow(path, mode):
    with ExitStack() as contexts:
        contexts.enter_context(sqlite_connection_scope(True))
        if mode == 'nested':
            contexts.enter_context(borrow(path))
        if mode == 'overflow':
            fill_cache(path)
        yield contexts.enter_context(borrow(path))


def inherited_exit(path, scenario, mode):
    with active_borrow(path, mode) as parent:
        cursor = parent.execute('SELECT value FROM facts ORDER BY value')
        assert cursor.fetchone()[0] == 0
        if scenario == 'transaction':
            parent.execute('INSERT INTO facts VALUES(99)')
        pid = os.fork()
        if pid == 0:
            return False  # 真正退出继承的两个 with，记录是否企图操作父物理连接。
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert parent.execute('SELECT count(*) FROM facts').fetchone()[0] == (6 if scenario == 'transaction' else 5)
        assert cursor.fetchone()[0] == 1
        parent.commit()
    with borrow(path) as fresh:
        expected = [0, 1, 2, 3, 4, 99] if scenario == 'transaction' else [0, 1, 2, 3, 4]
        assert [row[0] for row in fresh.execute('SELECT value FROM facts ORDER BY value')] == expected
    return True


def main():
    path, scenario, report = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    mode = sys.argv[4]
    with sqlite3.connect(path) as init:
        init.execute('CREATE TABLE facts(value INTEGER)')
        init.executemany('INSERT INTO facts VALUES(?)', [(n,) for n in range(5)])
    if inherited_exit(path, scenario, mode):
        facts = json.loads(report.read_text())
        assert facts['foreign_calls'] == [], facts
        assert facts['fresh_child_read'] == 5, facts
        print('active fork return and parent data verified')
        return
    with borrow(path) as fresh:
        assert type(fresh) is sqlite3.Connection
        count = fresh.execute('SELECT count(*) FROM facts').fetchone()[0]
    report.write_text(json.dumps({'foreign_calls': FOREIGN_CALLS, 'fresh_child_read': count}))
    os._exit(0)


if __name__ == '__main__':
    main()
