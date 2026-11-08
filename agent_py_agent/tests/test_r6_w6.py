"""隔离真实 SQLite、线程 Event 和子进程证明代次门；不执行在线换库。"""
import ctypes
import os
import sqlite3
import subprocess
import sys
import threading
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection, sqlite_error_details
from agent_py_agent.agent.io.sqlite_scope import (
    retire_scoped_database,
    scoped_connection,
    sqlite_connection_scope,
)
from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
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
    assert lock.stat().st_ino == identity
    if os.name != "nt":
        assert lock.stat().st_mode & 0o777 == 0o600


def test_nested_runtime_connect_and_shared_peer_release_only_after_close(database):
    connections = []
    try:
        connections.append(guard.guarded_runtime_connect(database, lambda: guard.guarded_runtime_connect(
            database, lambda: sqlite3.connect(database))))
        connections.append(guard.guarded_runtime_connect(database, lambda: sqlite3.connect(database)))
        assert all(conn.execute("SELECT 1").fetchone() == (1,) for conn in connections)
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("嵌套连接和同库连接未关闭时不能恢复")
        guard.close_runtime_connection(connections.pop(0))
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("另一个共享连接仍持有代次门")
    finally:
        for conn in connections:
            guard.close_runtime_connection(conn)
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


@pytest.fixture
def windows_lock_api(monkeypatch):
    api = SimpleNamespace(handle=ctypes.c_void_p(-2).value, calls=[], result=1, error=0, failure=None)
    size = 3 * ctypes.sizeof(ctypes.c_void_p) + 8
    assert ctypes.sizeof(guard._WindowsOverlapped) == size

    def lock_file(handle, flags, reserved, low, high, overlapped):
        api.calls.append((handle, flags, reserved, low, high, ctypes.string_at(overlapped, size)))
        return api.result

    # 实际经过 ctypes 的参数转换，验证宽句柄和归零结构；不把模拟当作 Windows 原生锁证明。
    prototype = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    monkeypatch.setattr(guard, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    monkeypatch.setattr(guard.json_io, "msvcrt", SimpleNamespace(get_osfhandle=lambda descriptor: api.handle))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=SimpleNamespace(LockFileEx=prototype(lock_file))),
                        raising=False)
    monkeypatch.setattr(ctypes, "GetLastError", lambda: api.error, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: api.failure, raising=False)
    return api


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("blocking", [False, True])
def test_windows_generation_lock_uses_shared_flags_and_full_handle(windows_lock_api, exclusive, blocking):
    api = windows_lock_api
    guard._lock_windows_generation(7, exclusive=exclusive, blocking=blocking)
    assert api.calls == [(api.handle, int(exclusive) * 2 + int(not blocking), 0, 1, 0,
                          bytes(ctypes.sizeof(guard._WindowsOverlapped)))]


@pytest.mark.parametrize("code", [33, 5, 32, 6, 8])
def test_windows_generation_lock_distinguishes_contention_and_io_errors(windows_lock_api, code):
    api = windows_lock_api
    api.result, api.error, api.failure = 0, code, OSError(code, "Win32 lock failure")
    with pytest.raises(BlockingIOError if code in {33, 5, 32} else OSError) as raised:
        guard._lock_windows_generation(7, exclusive=False, blocking=False)
    if code in {33, 5, 32}:
        assert raised.value.errno == code
    else:
        assert raised.value is api.failure
    assert raised.value.__cause__ is None


@pytest.fixture
def exclusive_generation_backend(monkeypatch):
    """用真实文件锁复现只支持 EX 的后端；不冒充 Windows 原生验证。"""
    attempts = []
    native = json_io.fcntl
    if native is not None:
        def lock(descriptor, mode):
            attempts.append(descriptor)
            native.flock(descriptor, (mode & ~native.LOCK_SH) | native.LOCK_EX)

        monkeypatch.setattr(json_io, "fcntl", SimpleNamespace(**(vars(native) | {"flock": lock})))
    else:
        native_lock = guard._lock_windows_generation

        def lock(descriptor, *, exclusive, blocking):
            attempts.append(descriptor)
            native_lock(descriptor, exclusive=True, blocking=blocking)

        monkeypatch.setattr(guard, "_lock_windows_generation", lock)
    return attempts


def test_runtime_constructor_query_transaction_and_cache_acquire_once(database, exclusive_generation_backend):
    attempts = exclusive_generation_backend
    with sqlite_connection_scope(False):
        repo = RuntimeRepository(database)
        assert len(attempts) == 1
        assert repo.get_task("absent") is None
        assert len(attempts) == 2
        with repo.transaction() as conn:
            conn.execute("INSERT INTO metadata(key,value) VALUES('r7-proof','committed')")
            assert len(guard._records()[1][guard._path(database)]) == 1
        assert len(attempts) == 3
        assert not guard._records()[1].get(guard._path(database))
        with sqlite_connection_scope(True):
            assert repo.get_task("absent") is None
            record = next(iter(guard._records()[1][guard._path(database)].values()))
            assert record["idle"] and len(attempts) == 4
            assert repo.get_task("absent") is None
            assert next(iter(guard._records()[1][guard._path(database)].values())) is record
            assert len(attempts) == 4
        with pytest.raises(sqlite3.ProgrammingError):
            record["conn"].execute("SELECT 1")
        assert not guard._records()[1].get(guard._path(database))
        with repo.transaction() as conn:
            assert conn.execute("SELECT value FROM metadata WHERE key='r7-proof'").fetchone()[0] == "committed"


@pytest.mark.parametrize("mode", ["direct", "existing_only", "read_only"])
def test_direct_runtime_connections_remain_pinned(database, exclusive_generation_backend, mode):
    RuntimeRepository(database)
    repo = RuntimeRepository(database, read_only=mode == "read_only", existing_only=mode == "existing_only")
    conn = repo._runtime_connect()
    try:
        record = guard._records()[0][id(conn)]
        assert record["conn"] is conn and record["path"] == guard._path(database)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
            with guard.runtime_db_generation_guard(database, exclusive=True):
                pytest.fail("直接连接未物理关闭前不能恢复")
    finally:
        guard.close_runtime_connection(conn)
    assert id(conn) not in guard._records()[0]
    with guard.runtime_db_generation_guard(database, exclusive=True):
        pass


@pytest.mark.parametrize("reuse", [False, True])
def test_generic_raw_factory_retains_one_physical_guard(database, exclusive_generation_backend, reuse):
    opened = []

    def connect():
        conn = sqlite3.connect(database, factory=TrackedSQLiteConnection)
        opened.append(conn)
        return conn

    with sqlite_connection_scope(reuse):
        for _ in range(2):
            with scoped_connection(database, False, connect) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
                assert len(guard._records()[1][guard._path(database)]) == 1
        assert len(opened) == (1 if reuse else 2)
        assert len(exclusive_generation_backend) == len(opened)
        if reuse:
            with scoped_connection(database, False, connect):
                with pytest.raises(guard.RuntimeDatabaseDeferred, match="generation_busy"):
                    with guard.runtime_db_generation_guard(database, exclusive=True):
                        pytest.fail("正在借用的缓存连接不能被恢复者关闭")
    assert not guard._records()[1].get(guard._path(database))
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("parent_exists", [False, True])
def test_limited_runtime_missing_source_creates_no_sidecar(tmp_path, read_only, parent_exists):
    path = tmp_path / "absent" / "runtime.db"
    if parent_exists:
        path.parent.mkdir()
    before = set(tmp_path.rglob("*"))
    repo = RuntimeRepository(path, read_only=read_only, existing_only=not read_only)
    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        repo.get_task("absent")
    assert set(tmp_path.rglob("*")) == before
