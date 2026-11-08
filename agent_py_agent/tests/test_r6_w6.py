"""隔离真实 SQLite、线程 Event 和子进程证明代次门；不执行在线换库。"""
import os
import sqlite3
import subprocess
import sys
import threading
from contextvars import copy_context

import pytest

from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection, sqlite_error_details
from agent_py_agent.agent.io.sqlite_scope import (
    retire_scoped_database,
    scoped_connection,
    sqlite_connection_scope,
)
from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db.sqlite_scope import (
    activate_sqlite_scope,
    close_persistent_sqlite_scope,
)
from agent_py_agent.tests.test_persistent_planner_sqlite_scope import _query
from agent_py_agent.tests.test_sqlite_scope_lifetime import _raw_lease, _raw_once


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime.db"
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else None
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts(value)")
    connection.close()
    yield path
    with guard.runtime_db_generation_guard(path, exclusive=True):
        pass
    if before is not None:
        assert len(os.listdir(fd_dir)) - before == 0


def test_event_transaction_blocks_recovery_and_new_admission(database):
    ready, release = threading.Event(), threading.Event()
    errors, published = [], []

    def writer():
        try:
            with sqlite_connection_scope(True), _raw_lease(database) as conn:
                conn.execute("INSERT INTO facts VALUES(1)")
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert ready.wait(10)
        assert retire_scoped_database(database)  # 仅 B 的本地状态，不能当作 A 已关闭。
        with pytest.raises(guard.RuntimeDatabaseDeferred):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                published.append(True)
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="recovery_pending"):
            _raw_once(database)
        assert not published
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    with guard.runtime_db_generation_guard(database, exclusive=True):
        published.append(True)
    assert published == [True] and _raw_once(database)


@pytest.mark.parametrize("planner", [False, True])
def test_creator_retires_idle_cache_and_planner_connections(database, planner):
    from agent_py_agent.agent.runtime_db.sqlite_scope import new_persistent_sqlite_scope

    if planner:
        scope = new_persistent_sqlite_scope()
        conn = _query(scope, database)
        assert retire_scoped_database(database)
        assert not scope.entries
        close_persistent_sqlite_scope(scope)
    else:
        with sqlite_connection_scope(True):
            with _raw_lease(database) as lease:
                conn = lease._target
            assert retire_scoped_database(database)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_planner_activation_retires_intent_without_foreign_close(database):
    from agent_py_agent.agent.runtime_db.sqlite_scope import new_persistent_sqlite_scope

    scope = new_persistent_sqlite_scope()
    conn = _query(scope, database)
    thread = threading.Thread(target=lambda: retire_scoped_database(database))
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and conn.execute("SELECT 1").fetchone() == (1,)
    with activate_sqlite_scope(scope):
        assert not scope.entries
        with pytest.raises(guard.RuntimeDatabaseDeferred):
            _raw_once(database)
    close_persistent_sqlite_scope(scope)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_subprocess_shared_connection_blocks_ex_until_normal_exit(database):
    script = """import sys
from pathlib import Path
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.tests.test_sqlite_scope_lifetime import _raw_lease
with sqlite_connection_scope(True):
    with _raw_lease(Path(sys.argv[1])) as conn:
        conn.execute('SELECT 1')
        print('ready', flush=True)
        sys.stdin.read(1)
"""
    env = dict(os.environ, MY_AGENT_HOME=os.environ["HOME"])
    process = subprocess.Popen([sys.executable, "-c", script, str(database)], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline() == "ready\n"
        with pytest.raises(guard.RuntimeDatabaseDeferred):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("仍有子进程 SH 时不能发布")
        output, error = process.communicate("x", timeout=10)
        assert process.returncode == 0, output + error
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_only_one_ex_and_copied_context_cannot_reuse_retired_ex(database):
    errors = []

    def contender():
        try:
            with guard.runtime_db_generation_guard(database, exclusive=True):
                errors.append("second publisher")
        except guard.RuntimeDatabaseDeferred:
            pass

    with guard.runtime_db_generation_guard(database, exclusive=True):
        copied = copy_context()
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(10)
        assert not thread.is_alive() and not errors
        with _raw_lease(database) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    guard.request_runtime_db_recovery(database)
    with pytest.raises(guard.RuntimeDatabaseDeferred):
        copied.run(_raw_once, database)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_failed_physical_close_keeps_shared_proof_until_creator_retries(database):
    class CloseFailure(TrackedSQLiteConnection):
        fail_close = True

        def close(self):
            if self.fail_close:
                raise RuntimeError("close not performed")
            super().close()

    with pytest.raises(RuntimeError), sqlite_connection_scope(True):
        with scoped_connection(database, False, lambda: sqlite3.connect(database, factory=CloseFailure)):
            pass
    with pytest.raises(guard.RuntimeDatabaseDeferred) as failure:
        with guard.runtime_db_generation_guard(database, exclusive=True):
            pytest.fail("物理 close 未确认时不能发布")
    assert sqlite_error_details(failure.value)["status"] == "deferred"
    CloseFailure.fail_close = False
    assert retire_scoped_database(database)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


@pytest.mark.parametrize("error", [TimeoutError("deadline"), sqlite3.OperationalError("busy")])
def test_factory_failure_keeps_original_error_and_releases_admission(database, error):
    def fail():
        raise error

    with pytest.raises(type(error)) as raised:
        with scoped_connection(database, False, fail):
            pass
    assert raised.value is error
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_structural_statement_marks_recovery_without_replaying_body(database):
    with sqlite_connection_scope(True), _raw_lease(database) as conn:
        error = sqlite3.DatabaseError("structural sample")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        conn._target.record_lease_error(error)
        assert guard.runtime_db_recovery_pending(database)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_real_busy_is_resource_contention_and_never_corruption(database):
    writer = sqlite3.connect(database)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with scoped_connection(database, False, lambda: sqlite3.connect(database, timeout=0)) as conn:
            with pytest.raises(sqlite3.OperationalError) as raised:
                conn.execute("INSERT INTO facts VALUES(2)")
        assert sqlite_error_details(raised.value)["category"] == "busy"
        assert not guard.runtime_db_recovery_pending(database)
    finally:
        writer.rollback()
        writer.close()


def test_close_error_after_actual_close_releases_guard_without_hiding_error(database):
    class ClosedThenError(TrackedSQLiteConnection):
        def close(self):
            super().close()
            raise RuntimeError("close already acknowledged")

    with pytest.raises(RuntimeError, match="already acknowledged"), sqlite_connection_scope(True):
        with scoped_connection(database, False, lambda: sqlite3.connect(database, factory=ClosedThenError)):
            pass
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


def test_generation_guard_preserves_body_error_and_lock_inode(database):
    lock = database.with_name(database.name + ".generation.lock")
    with guard.runtime_db_generation_guard(database):
        identity = lock.stat().st_ino
    error = BlockingIOError("body resource unavailable")
    with pytest.raises(BlockingIOError) as raised:
        with guard.runtime_db_generation_guard(database, exclusive=True):
            raise error
    assert raised.value is error and guard.runtime_db_recovery_pending(database)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass
    assert lock.stat().st_ino == identity and lock.stat().st_mode & 0o777 == 0o600
