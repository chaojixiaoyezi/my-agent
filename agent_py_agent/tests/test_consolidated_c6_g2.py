"""Wave 3 分片 c6 第 2 组整合测试（SQLite 代次与损坏恢复、JSONL 追尾、本地存储只读与子代理索引派工回归）。"""
import copy
import ctypes
import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import startup_recovery as startup
from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
    _dispatch_context_from_params,
    _finalize_scoped_tasks,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import (
    DispatchParams,
    RunnerBatchContext,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.refs import dispatch_scope_tasks
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    execute_runner_jobs,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.service import (
    make_capability_route_records,
    update_pending_work_state,
)
from agent_py_agent.agent.agent_core.orchestration.tools.cancel import _resolve_run_ids
from agent_py_agent.agent.agent_core.orchestration.tools.capability import _known_children_hint
from agent_py_agent.agent.agent_core.planner_service import (
    PlannerStateParams,
    _collect_parent_planner_context,
)
from agent_py_agent.agent.agent_core.runner.dispatch import (
    RunnerCandidatePolicy,
    _is_dispatch_runner_candidate,
)
from agent_py_agent.agent.agent_core.services import watch_service as watch
from agent_py_agent.agent.agent_core.tool_loop.completion import _delegated_work_facts
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.ingestion import source_worker as source
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection, sqlite_error_details
from agent_py_agent.agent.io.sqlite_scope import (
    retire_scoped_database,
    scoped_connection,
    sqlite_connection_scope,
)
from agent_py_agent.agent.local_storage import schema
from agent_py_agent.agent.local_storage.control_plane_codec import (
    AGENT_RUN_UPSERT_SQL,
    agent_run_values,
)
from agent_py_agent.agent.local_storage.control_plane_models import (
    AgentRunRecord,
    AgentRuntimeQueryContext,
    TaskRollupRecord,
)
from agent_py_agent.agent.memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES
from agent_py_agent.agent.plugin_host import subagents_v2_handlers as plugin
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import (
    SALVAGE_REQUEST_SUFFIX,
    note_runtime_db_corruption,
    runtime_db_identity,
)
from agent_py_agent.agent.runtime_db.sqlite_scope import (
    activate_sqlite_scope,
    close_persistent_sqlite_scope,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager, SubagentRunIndexUnavailable
from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex
from agent_py_agent.agent.subagents.services.persistence.service import SubAgentPersistenceService
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
from agent_py_agent.cli import gateway_process
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.tests import test_gap_w1_wire_roundtrip as roundtrip
from agent_py_agent.tests import test_r5db_jsonl_append_tail_seal as tail_seal
from agent_py_agent.tests.support.fd_lifetime import assert_fd_unchanged, fd_baseline
from agent_py_agent.tests.test_local_store_coordinator_io import make_store
from agent_py_agent.tests.test_persistent_planner_sqlite_scope import _query
from agent_py_agent.tests.test_r7_a_w2 import _task
from agent_py_agent.tests.test_rdb_w3_runtime_db_salvage import _damaged_copy, _source
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params
from agent_py_agent.tests.test_sqlite_scope_lifetime import _raw_lease, _raw_once
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager

wire = roundtrip.wire
append_records = tail_seal.append_records


# === test_r6_w6.py: 隔离真实 SQLite、线程 Event 和子进程证明代次门；不执行在线换库。 ===

@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime.db"
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = fd_baseline() if os.path.isdir(fd_dir) else None
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE facts(value)")
    connection.close()
    yield path
    with guard.runtime_db_generation_guard(path, exclusive=True):
        pass
    if before is not None:
        assert_fd_unchanged(before)


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
            # 替身只将SH准入改作真实EX竞争；LOCK_UN必须原样传递，UN|EX在Linux是非法模式。
            if mode & native.LOCK_UN:
                return native.flock(descriptor, mode)
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


# === test_r6_w7.py: 真实损坏副本上的恢复、代次排空、持久阶段与 Gateway 启动回归。 ===

def _blocked(path):
    with pytest.raises(guard.RuntimeDatabaseDeferred):
        with guard.runtime_db_generation_guard(path, exclusive=True):
            pytest.fail("物理连接仍存活，不能放行换库")


@pytest.mark.parametrize("mode", ["existing_only", "transaction", "read_only", "source"])
def test_real_runtime_connections_block_ex_until_physical_close(tmp_path, monkeypatch, mode):
    source = _source(tmp_path / "runtime.db")
    if mode == "source":
        original = salvage._copy_table

        def copy(conn, table):
            _blocked(source)
            return original(conn, table)

        monkeypatch.setattr(salvage, "_copy_table", copy)
        assert salvage.salvage_runtime_db(source, tmp_path / "candidate.db").ok
    else:
        repo = RuntimeRepository(source, read_only=mode == "read_only", existing_only=mode == "existing_only")
        context = repo.transaction() if mode == "transaction" else repo._runtime_connection()
        with context:
            _blocked(source)
    with guard.runtime_db_generation_guard(source, exclusive=True):
        pass


@pytest.fixture
def damaged(tmp_path):
    source = _source(tmp_path / "source/runtime.db", events=1024)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE tool_operations SET status='UNKNOWN'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return _damaged_copy(source, tmp_path / "owner/runtime.db", table="runtime_events", tail=True)


def _request(db):
    return Path(f"{db}{SALVAGE_REQUEST_SUFFIX}")


def _assert_authority(db, watermark=1024):
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM tool_operations").fetchone()[0] == "UNKNOWN"
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='runtime_events'").fetchone()[0] >= watermark


def test_startup_heals_marker_and_retains_raw_backup(damaged):
    before = damaged.read_bytes()
    identity = runtime_db_identity(damaged)
    _request(damaged).touch()
    receipt = salvage.heal_runtime_db_at_startup(damaged)
    assert receipt.ok and receipt.events_error and 0 < receipt.events_copied < 1024
    assert runtime_db_identity(damaged) != identity and not _request(damaged).exists()
    report = json.loads(Path(f"{damaged}.recovery-result.json").read_text())
    assert damaged.with_name(report["backup"]).read_bytes() == before
    assert report["events_loss"] is True
    _assert_authority(damaged)


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("transaction", [False, True])
def test_online_failure_is_not_replayed_and_next_query_succeeds(damaged, reuse, transaction):
    with sqlite_connection_scope(reuse):
        repo = RuntimeRepository(damaged)
        entered = []
        with pytest.raises(sqlite3.DatabaseError) as caught:
            with (repo.transaction() if transaction else repo._runtime_connection()) as conn:
                entered.append(True)
                conn.execute("SELECT * FROM runtime_events").fetchall()
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
        assert caught.value.runtime_db_recovery["status"] == "recovered"
        assert entered == [True]
        with repo._runtime_connection() as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    _assert_authority(damaged)


def test_no_request_does_not_open_database(damaged, monkeypatch):
    monkeypatch.setattr(salvage.sqlite3, "connect", lambda *a, **kw: pytest.fail("无请求不能扫描库"))
    assert salvage.heal_runtime_db_at_startup(damaged) is None


@pytest.mark.parametrize("limited", ["read_only", "existing_only"])
def test_readers_report_corruption_without_recovery_writes(damaged, monkeypatch, limited):
    repo = RuntimeRepository(damaged, **{limited: True})
    monkeypatch.setattr(salvage, "recover_runtime_database", lambda *a, **kw: pytest.fail("只读探针不能获恢复权"))
    with sqlite_connection_scope(True), pytest.raises(sqlite3.DatabaseError) as caught:
        with repo._runtime_connection() as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    assert caught.value.runtime_db_recovery["status"] == "requires_recovery"
    assert not _request(damaged).exists() and not guard.runtime_db_recovery_pending(damaged)


@pytest.mark.parametrize("table", [None, "tasks"])
def test_unverifiable_core_is_retained_and_deduplicated(tmp_path, monkeypatch, table):
    source = _source(tmp_path / "source/runtime.db")
    if table:
        db = _damaged_copy(source, tmp_path / "owner/runtime.db", table=table)
    else:
        db = source
        with db.open("r+b") as stream:
            stream.write(b"not a sqlite header!")
    identity, raw = runtime_db_identity(db), db.read_bytes()
    _request(db).touch()
    first = salvage.recover_runtime_database(db, expected_identity=identity)
    assert first["status"] == "requires_recovery", first
    assert db.with_name(first["candidate"]).exists()
    monkeypatch.setattr(salvage, "salvage_runtime_db", lambda *a, **kw: pytest.fail("同源不可重复抢救"))
    second = salvage.recover_runtime_database(db, expected_identity=identity)
    assert second["status"] == "requires_recovery"
    assert db.read_bytes() == raw and runtime_db_identity(db) == identity and _request(db).exists()
    healthy = RuntimeRepository(tmp_path / "healthy/runtime.db")
    with healthy._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("error", [sqlite3.OperationalError("no such table"), PermissionError("denied")])
def test_noncorruption_never_requests_salvage(tmp_path, monkeypatch, error):
    db = _source(tmp_path / "runtime.db")
    repo = RuntimeRepository(db)

    @contextmanager
    def failing():
        raise error
        yield

    monkeypatch.setattr(repo, "_runtime_connection_scope", failing)
    monkeypatch.setattr(salvage, "recover_runtime_database", lambda *a, **kw: pytest.fail("错误分类越权"))
    with pytest.raises(type(error)) as caught:
        with repo._runtime_connection():
            pass
    assert caught.value is error and not _request(db).exists()


def test_gateway_startup_hook_uses_owner_and_defers(tmp_path, monkeypatch, capsys):
    seen = []

    def failing(path):
        seen.append(Path(path))
        raise BlockingIOError("attached")

    monkeypatch.setattr(salvage, "heal_runtime_db_at_startup", failing)
    args = SimpleNamespace(config=None, _gateway_frozen_config=AgentConfig(my_agent_home=str(tmp_path / "home")))
    gateway_process._heal_runtime_db_before_agent(args)
    assert seen == [tmp_path / "home/owners/local/main/runtime.db"]
    assert "runtime_db_salvage_deferred error=BlockingIOError" in capsys.readouterr().err


def test_transient_wal_signal_waits_for_physical_close_then_clears(tmp_path, damaged):
    db = _source(tmp_path / "healthy/runtime.db")
    repo = RuntimeRepository(db)
    identity = runtime_db_identity(db)
    with pytest.raises(sqlite3.DatabaseError) as actual:
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    with repo._runtime_connection() as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES('wal-state','preserve')")
        conn.commit()
        note_runtime_db_corruption(db, actual.value)
        report = salvage.recover_runtime_database(db, expected_identity=identity)
        assert report["status"] == "deferred" and _request(db).exists()
        assert runtime_db_identity(db) == identity
    assert salvage.heal_runtime_db_at_startup(db) is None
    assert runtime_db_identity(db) == identity and not _request(db).exists()
    assert not list(db.parent.glob("*.damaged-*")) and not list(db.parent.glob("*.salvage-*"))
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='wal-state'").fetchone()[0] == "preserve"
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='runtime_events'").fetchone()[0] == 11


def test_two_threads_only_publish_once(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    entered, release = threading.Event(), threading.Event()
    original = salvage._copy_backup
    results = []

    def held(*args):
        entered.set()
        assert release.wait(15)
        return original(*args)

    monkeypatch.setattr(salvage, "_copy_backup", held)
    worker = threading.Thread(target=lambda: results.append(salvage.recover_runtime_database(damaged, expected_identity=identity)))
    worker.start()
    try:
        assert entered.wait(15)
        second = salvage.recover_runtime_database(damaged, expected_identity=identity)
        assert second["status"] == "deferred"
        assert runtime_db_identity(damaged) == identity
    finally:
        release.set()
        worker.join(15)
    assert not worker.is_alive() and results[0]["status"] == "recovered", results
    assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "already_recovered"
    assert len([p for p in damaged.parent.glob("*.damaged-*") if not p.name.endswith(("-wal", "-shm", "-journal"))]) == 1
    _assert_authority(damaged)


def _child(code, db, *arguments):
    return subprocess.Popen([sys.executable, "-c", code, str(db), *arguments],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _stop_child(child):
    if child.stdin and not child.stdin.closed:
        child.stdin.close()
        child.stdin = None
    output, error = child.communicate(timeout=30)
    assert child.returncode == 0, (output, error)
    return output


@pytest.mark.parametrize("managed", [False, True])
def test_idle_reader_in_other_process_defers_swap(damaged, managed):
    identity = runtime_db_identity(damaged)
    code = """
import sqlite3, sys
from pathlib import Path
from contextlib import closing
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
db = Path(sys.argv[1])
context = RuntimeRepository(db, read_only=True)._runtime_connection() if sys.argv[2] == 'managed' else closing(sqlite3.connect(db))
with context as conn:
    conn.execute('SELECT count(*) FROM sqlite_master').fetchall()
    print('attached', flush=True)
    sys.stdin.read()
"""
    holder = _child(code, damaged, "managed" if managed else "raw")
    try:
        assert holder.stdout.readline().strip() == "attached"
        result = salvage.recover_runtime_database(damaged, expected_identity=identity)
        assert result["status"] == "deferred", result
        assert runtime_db_identity(damaged) == identity
        assert not list(damaged.parent.glob("*.damaged-*"))
    finally:
        _stop_child(holder)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered", result
    _assert_authority(damaged)


def test_dispatch_child_leaves_durable_request_for_host(damaged):
    identity = runtime_db_identity(damaged)
    with RuntimeRepository(damaged)._runtime_connection() as holder:
        holder.execute("SELECT count(*) FROM tasks").fetchall()
        child = _child("""
import sqlite3, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
try:
    with RuntimeRepository(Path(sys.argv[1]))._runtime_connection() as conn:
        conn.execute('SELECT * FROM runtime_events').fetchall()
except sqlite3.DatabaseError as error:
    assert error.runtime_db_recovery['status'] == 'deferred'
else:
    raise AssertionError('expected real corruption')
""", damaged)
        _stop_child(child)
        assert _request(damaged).is_file() and runtime_db_identity(damaged) == identity
    with RuntimeRepository(damaged)._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert runtime_db_identity(damaged) != identity
    _assert_authority(damaged)


@pytest.mark.parametrize("phase", ["backing_up", "backed_up", "building_candidate", "candidate_ready", "publishing", "published"])
def test_process_exit_at_durable_phase_resumes(damaged, phase):
    identity = runtime_db_identity(damaged)
    child = _child("""
import os, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.schema import runtime_db_identity
db, phase = Path(sys.argv[1]), sys.argv[2]
original = salvage._save_state
def crash(db, state, stage):
    original(db, state, stage)
    if stage == phase:
        os._exit(73)
salvage._save_state = crash
salvage.recover_runtime_database(db, expected_identity=runtime_db_identity(db))
raise AssertionError('did not reach crash boundary')
""", damaged, phase)
    output, error = child.communicate(timeout=30)
    assert child.returncode == 73, (output, error)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered", result
    assert not _request(damaged).exists() and not guard.runtime_db_recovery_pending(damaged)
    _assert_authority(damaged)


def test_single_publish_rename_failure_keeps_journal_and_resumes(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    replace = salvage.os.replace
    failures = []

    def fail_once(source, destination):
        if Path(destination) == damaged and not failures:
            failures.append(True)
            raise PermissionError("single rename failure")
        return replace(source, destination)

    monkeypatch.setattr(salvage.os, "replace", fail_once)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "deferred" and result["phase"] == "publishing", result
    assert runtime_db_identity(damaged) == identity and guard.runtime_db_recovery_pending(damaged)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "recovered" and failures == [True], result
    _assert_authority(damaged)


def test_new_generation_is_never_overwritten(damaged, tmp_path, monkeypatch):
    identity = runtime_db_identity(damaged)
    replacement = _source(tmp_path / "new/runtime.db")
    new_bytes = replacement.read_bytes()
    publish = salvage._publish_candidate

    def moved(db, state):
        replacement.replace(db)
        return publish(db, state)

    monkeypatch.setattr(salvage, "_publish_candidate", moved)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "superseded", result
    assert damaged.read_bytes() == new_bytes


def test_candidate_generation_failure_preserves_source_and_candidate(damaged, monkeypatch):
    identity, before = runtime_db_identity(damaged), damaged.read_bytes()
    original = salvage.salvage_runtime_db

    def newer(*args, **kwargs):
        receipt = original(*args, **kwargs)
        with closing(sqlite3.connect(receipt.target)) as conn:
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','999999')")
            conn.commit()
        return receipt

    monkeypatch.setattr(salvage, "salvage_runtime_db", newer)
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "requires_recovery", result
    assert runtime_db_identity(damaged) == identity and damaged.read_bytes() == before
    assert damaged.with_name(result["candidate"]).exists()


def test_two_processes_only_publish_once(damaged):
    identity = runtime_db_identity(damaged)
    child = _child("""
import json, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.schema import runtime_db_identity
db = Path(sys.argv[1])
original = salvage._copy_backup
def held(*args):
    print('exclusive', flush=True)
    sys.stdin.readline()
    return original(*args)
salvage._copy_backup = held
print(json.dumps(salvage.recover_runtime_database(db, expected_identity=runtime_db_identity(db))), flush=True)
""", damaged)
    try:
        assert child.stdout.readline().strip() == "exclusive"
        assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "deferred"
    finally:
        output = _stop_child(child)
    assert json.loads(output)["status"] == "recovered"
    assert salvage.recover_runtime_database(damaged, expected_identity=identity)["status"] == "already_recovered"
    assert len([p for p in damaged.parent.glob("*.damaged-*") if not p.name.endswith(("-wal", "-shm", "-journal"))]) == 1
    _assert_authority(damaged)


def test_real_client_gateway_envelope_then_recovered_request(wire, damaged, monkeypatch):
    from agent_py_agent.agent.settings import model_scope

    repo = RuntimeRepository(damaged)
    entered = []

    def real_database_call():
        entered.append(True)
        with repo._runtime_connection() as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()

    original = model_scope._profile_backend

    def backend(host, config):
        host.backend_hook = real_database_call
        return original(host, config)

    monkeypatch.setattr(model_scope, "_profile_backend", backend)
    status, _, raw = roundtrip.request(wire, "/api/llm/test", {})
    envelope = json.loads(raw)
    assert status == 500 and envelope["error"]["code"] == "HOST_NOT_READY", envelope
    assert entered == [True]
    status, _, raw = roundtrip.request(wire, "/api/llm/test", {})
    envelope = json.loads(raw)
    assert status == 200 and envelope["ok"] is True and "error" not in envelope, envelope
    assert entered == [True, True]
    _assert_authority(damaged)


def test_gateway_startup_hook_really_heals_before_construction(damaged, tmp_path, capsys):
    home = tmp_path / "gateway-home"
    db = home / "owners/local/main/runtime.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(damaged.read_bytes())
    identity = runtime_db_identity(db)
    _request(db).touch()
    args = SimpleNamespace(config=None, _gateway_frozen_config=AgentConfig(my_agent_home=str(home)))
    gateway_process._heal_runtime_db_before_agent(args)
    assert "runtime_db_salvaged" in capsys.readouterr().err
    assert runtime_db_identity(db) != identity and not _request(db).exists()
    RuntimeRepository(db)
    _assert_authority(db)


def test_startup_with_destroyed_event_root_preserves_authority(tmp_path):
    source = _source(tmp_path / "source/runtime.db", events=1024)
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE tool_operations SET status='UNKNOWN'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = _damaged_copy(source, tmp_path / "owner/runtime.db", table="runtime_events")
    _request(db).touch()
    receipt = salvage.heal_runtime_db_at_startup(db)
    assert receipt.ok and receipt.events_copied == 0 and receipt.events_error
    _assert_authority(db)


def test_actual_busy_is_not_corruption(tmp_path):
    db = _source(tmp_path / "runtime.db")
    with closing(sqlite3.connect(db)) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            with RuntimeRepository(db, existing_only=True).transaction() as conn:
                conn.execute("UPDATE tasks SET goal='busy'")
        assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        assert not _request(db).exists() and not hasattr(caught.value, "runtime_db_recovery")


def test_failed_physical_close_defers_until_creator_can_close(tmp_path, monkeypatch):
    db = _source(tmp_path / "runtime.db")
    _request(db).touch()
    identity = runtime_db_identity(db)
    connect = sqlite3.connect
    faults = []

    class CloseOnce(sqlite3.Connection):
        def close(self):
            if not faults:
                faults.append(True)
                raise sqlite3.OperationalError("single close failure")
            return super().close()

    def traced(*args, **kwargs):
        kwargs["factory"] = CloseOnce
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)
    before = len(os.listdir("/dev/fd")) if os.name != "nt" else None
    first = salvage.recover_runtime_database(db, expected_identity=identity)
    assert first["status"] == "deferred" and guard.runtime_db_recovery_pending(db)
    assert runtime_db_identity(db) == identity
    second = salvage.recover_runtime_database(db, expected_identity=identity)
    assert second["status"] == "healthy" and faults == [True], second
    if before is not None:
        assert len(os.listdir("/dev/fd")) - before == 0


def test_retained_raw_wal_bytes_match_backup_before_salvage(damaged, monkeypatch):
    writer = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("UPDATE metadata SET value='cold-wal-authority' WHERE key='fixture-marker'")
conn.commit()
os._exit(0)
""", str(damaged)], capture_output=True, text=True, timeout=30)
    assert writer.returncode == 0, writer.stderr
    original_wal = Path(f"{damaged}-wal").read_bytes()
    assert original_wal
    original = salvage.salvage_runtime_db
    saved = []

    def verify(source, target, **kwargs):
        state = json.loads(_request(source).read_text())
        backup = source.with_name(state["backup"])
        for suffix in ("", "-wal", "-shm", "-journal"):
            hot = Path(f"{source}{suffix}")
            if hot.exists():
                assert Path(f"{backup}{suffix}").read_bytes() == hot.read_bytes()
                saved.append(suffix)
        assert Path(f"{backup}-wal").read_bytes() == original_wal
        return original(source, target, **kwargs)

    monkeypatch.setattr(salvage, "salvage_runtime_db", verify)
    result = salvage.recover_runtime_database(damaged, expected_identity=runtime_db_identity(damaged))
    assert result["status"] == "recovered" and "" in saved and "-wal" in saved, result
    with closing(sqlite3.connect(damaged)) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='fixture-marker'").fetchone()[0] == "cold-wal-authority"


def test_resume_does_not_overwrite_same_inode_new_generation(damaged, monkeypatch):
    identity = runtime_db_identity(damaged)
    replace = salvage.os.replace

    def fail_publish(source, destination):
        if Path(destination) == damaged:
            raise PermissionError("single publication boundary")
        return replace(source, destination)

    monkeypatch.setattr(salvage.os, "replace", fail_publish)
    first = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert first["phase"] == "publishing" and first["status"] == "deferred"
    monkeypatch.setattr(salvage.os, "replace", replace)
    # 单次确定性交错：未受管的新代写者修改同 inode；不得用旧候选盖回。
    with closing(sqlite3.connect(damaged)) as conn:
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','2')")
        conn.commit()
    current = damaged.read_bytes()
    result = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert result["status"] == "deferred" and result["reason"] == "source_contents_changed", result
    assert damaged.read_bytes() == current and runtime_db_identity(damaged) == identity


def test_failed_candidate_close_retires_before_next_attempt(damaged, monkeypatch):
    connect, failures = sqlite3.connect, []

    class CandidateClose(sqlite3.Connection):
        def close(self):
            if not failures:
                failures.append(True)
                raise sqlite3.OperationalError("single candidate close failure")
            return super().close()

    def traced(address, *args, **kwargs):
        if ".salvage-" in str(address) and str(address).endswith("?mode=rw"):
            kwargs["factory"] = CandidateClose
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", traced)
    identity = runtime_db_identity(damaged)
    before = len(os.listdir("/dev/fd")) if os.name != "nt" else None
    first = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert first["status"] == "deferred" and failures == [True], first
    assert runtime_db_identity(damaged) == identity
    second = salvage.recover_runtime_database(damaged, expected_identity=identity)
    assert second["status"] == "recovered", second
    if before is not None:
        assert len(os.listdir("/dev/fd")) - before == 0
    _assert_authority(damaged)


def test_invalid_marker_keeps_original_corruption_and_source(damaged):
    before = damaged.read_bytes()
    with pytest.raises(sqlite3.DatabaseError) as caught:
        with closing(sqlite3.connect(damaged)) as conn:
            conn.execute("SELECT * FROM runtime_events").fetchall()
    _request(damaged).write_text("[]")
    note_runtime_db_corruption(damaged, caught.value)
    assert caught.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CORRUPT
    assert isinstance(caught.value.sqlite_recovery_error, ValueError)
    result = salvage.recover_runtime_database(damaged, expected_identity=runtime_db_identity(damaged))
    assert result["status"] == "requires_recovery"
    assert damaged.read_bytes() == before and _request(damaged).read_text() == "[]"


# === test_r6_w8.py: 继承验证：两条追加路径都须保留单次 EIO，并在重试时正确封住原尾。 ===

@pytest.mark.parametrize("tail", [b'{"half": ', b'{"complete": 2}'])
def test_inherited_tail_and_eio(tmp_path, monkeypatch, append_records, tail):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"keep": 1}\n' + tail)
    failure = OSError(errno.EIO, "injected source-open failure")
    original_open, failed = os.open, []

    def fail_once(open_path, flags, *args, **kwargs):
        if (os.fspath(open_path) == os.fspath(path)
                and flags & os.O_ACCMODE == os.O_RDWR and not failed):
            failed.append(True)
            raise failure
        return original_open(open_path, flags, *args, **kwargs)

    monkeypatch.setattr(json_io.os, "open", fail_once)
    with pytest.raises(OSError) as caught:
        append_records(path, [{"new": 3}])
    assert caught.value is failure and failed == [True]
    assert path.read_bytes() == b'{"keep": 1}\n' + tail and not tail_seal._torn(path)
    append_records(path, [{"new": 3}])
    report = json_io.read_jsonl_objects_report(path)
    expected = [{"keep": 1}, *([{"complete": 2}] if tail == b'{"complete": 2}' else []), {"new": 3}]
    assert report.load_errors == [] and report.records == expected


# === test_r7_a_w1.py: 纯控制面查询不借写权限，真实 SQLite 的未知和 WAL 语义保持。 ===

@pytest.fixture
def populated(tmp_path):
    store, door = make_store(tmp_path / "store", enable_fts=False)
    run = AgentRunRecord("child", "task", parent_run_id="parent", depth=1, status="RUNNING",
                         created_at=1.0, updated_at=1.0)
    rollup = TaskRollupRecord("task", status="RUNNING", running_agents=1, updated_at=1.0)
    with global_write_commit_scope(door):
        store.upsert_agent_run(run)
        store.upsert_task_rollup(rollup)
    return store, door, run, rollup


def test_public_tree_read_needs_no_writer_scope(populated):
    store, door, run, rollup = populated
    calls = door.calls
    report = store.list_agent_tree("task")
    assert report.runs == [run] and report.rollup == rollup
    assert store.get_agent_run(run.run_id) == run
    assert store.get_task_rollup("task") == rollup
    assert store.list_subtree(run.run_id).runs == [run]
    assert store.list_blocked_runs("task") == []
    assert store.query_agent_runtime(AgentRuntimeQueryContext(root_task_id="task")).report == report
    assert door.calls == calls


def test_passive_tree_has_only_selects_and_no_filesystem_writers(populated, monkeypatch, record_property):
    store, door, run, _rollup = populated
    actual_connect = sqlite3.connect
    connections, statements = [], []

    def connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        connections.append((args, kwargs, conn))
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*args, **kwargs):
        pytest.fail("passive query entered a filesystem/schema writer")

    # 保留一个真实 WAL 连接，避免把 SQLite 自身首读创建共享内存混入应用写者计数。
    with closing(actual_connect(store.db_path)) as keeper:
        keeper.execute("SELECT run_id FROM legacy_agent_runs").fetchall()
        before = {str(path.relative_to(store.root)): (path.stat().st_mode, path.stat().st_size,
                  path.stat().st_mtime_ns) for path in store.root.rglob("*")}
        with monkeypatch.context() as patch:
            patch.setattr(schema.sqlite3, "connect", connect)
            patch.setattr(schema, "ensure_private_dir", forbidden)
            patch.setattr(schema, "_private_sqlite_files", forbidden)
            patch.setattr(store, "_ensure_schema", forbidden)
            patch.setattr(schema.os, "chmod", forbidden)
            patch.setattr(schema.os, "fchmod", forbidden, raising=False)
            report = store.list_agent_tree("task")
        after = {str(path.relative_to(store.root)): (path.stat().st_mode, path.stat().st_size,
                 path.stat().st_mtime_ns) for path in store.root.rglob("*")}
        assert report.runs == [run] and before == after
    # 树和汇总共享一次只读连接，两条查询的写权限与关闭证明仍分别核验。
    assert len(connections) == 1 and len(statements) == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    for args, kwargs, conn in connections:
        assert "mode=ro" in args[0] and "immutable" not in args[0] and kwargs["uri"] is True
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    record_property("passive_connects", len(connections))
    record_property("passive_sql_statements", statements)
    record_property("filesystem_changed_paths", [])


def test_passive_reader_does_not_grant_existing_writers_authority(populated):
    store, _door, run, rollup = populated
    for write, value in ((store.upsert_agent_run, replace(run, status="DONE")),
                         (store.upsert_task_rollup, replace(rollup, status="DONE"))):
        with pytest.raises(GlobalWriteCommitUnavailable):
            write(value)
    with pytest.raises(GlobalWriteCommitUnavailable):
        store.count_records()
    with store._passive_connection() as conn, pytest.raises(sqlite3.OperationalError) as caught:
        conn.execute("DELETE FROM legacy_agent_runs")
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    assert store.get_agent_run(run.run_id) == run


@pytest.mark.parametrize("missing", ["database", "directory"])
def test_missing_passive_store_is_not_recreated(populated, missing):
    store, _door, _run, _rollup = populated
    if missing == "database":
        store.db_path.unlink()
    else:
        shutil.rmtree(store.root)
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        store.list_agent_tree("task")
    assert not store.db_path.exists()
    if missing == "directory":
        assert not store.root.exists()


def test_corruption_is_explicit_and_does_not_repair(populated):
    store, _door, _run, _rollup = populated
    store.db_path.write_bytes(b"not a SQLite database" * 256)
    before = store.db_path.stat()
    with pytest.raises(sqlite3.DatabaseError) as caught:
        store.list_agent_tree("task")
    assert sqlite_error_details(caught.value)["category"] == "structural"
    after = store.db_path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_unavailable_source_preserves_the_original_error(populated, monkeypatch):
    store, _door, _run, _rollup = populated
    error = PermissionError("SQLite source unavailable")

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(schema.sqlite3, "connect", unavailable)
    with pytest.raises(PermissionError) as caught:
        store.list_agent_tree("task")
    assert caught.value is error and caught.value.__cause__ is None


@pytest.mark.parametrize("kind", ["database_link", "wal_link", "hardlink"])
def test_non_plain_sqlite_files_are_not_read_or_repaired(populated, kind):
    store, _door, _run, _rollup = populated
    if kind == "hardlink":
        os.link(store.db_path, store.db_path.with_suffix(".link"))
    elif kind == "database_link":
        target = store.db_path.with_suffix(".saved")
        store.db_path.rename(target)
        store.db_path.symlink_to(target)
    else:
        target = store.root / "other-wal"
        target.touch()
        wal = store.db_path.with_name(store.db_path.name + "-wal")
        wal.unlink(missing_ok=True)
        wal.symlink_to(target)
    with pytest.raises(ValueError, match="SQLite"):
        store.list_agent_tree("task")


def test_busy_store_does_not_become_a_healthy_empty_tree(populated):
    store, door, run, _rollup = populated
    with global_write_commit_scope(door), store._connection() as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.list_agent_tree("task")
        assert sqlite_error_details(caught.value)["category"] in {"busy", "locked"}
        writer.rollback()
    assert store.list_agent_tree("task").runs == [run]


def test_wal_reader_sees_committed_state_while_writer_is_active(populated):
    store, door, run, _rollup = populated
    ready, release = threading.Event(), threading.Event()
    errors = []

    def write():
        try:
            with global_write_commit_scope(door), store._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE legacy_agent_runs SET status='DONE' WHERE run_id=?", (run.run_id,))
                ready.set()
                assert release.wait(10)
                conn.commit()
        except BaseException as error:
            errors.append(error)

    with closing(sqlite3.connect(store.db_path)) as keeper:
        assert keeper.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        thread = threading.Thread(target=write)
        thread.start()
        try:
            assert ready.wait(10)
            assert store.get_agent_run(run.run_id).status == "RUNNING"
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and not errors
        assert store.get_agent_run(run.run_id).status == "DONE"


@pytest.mark.parametrize("failure", [False, True])
def test_passive_connections_close_on_normal_and_exceptional_paths(populated, failure, record_property):
    store, _door, _run, _rollup = populated
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else None
    for _ in range(16):
        if failure:
            with pytest.raises(sqlite3.OperationalError), store._passive_connection() as conn:
                conn.execute("SELECT * FROM missing_table")
        else:
            with store._passive_connection() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    if before is not None:
        delta = len(os.listdir(fd_dir)) - before
        assert delta == 0
        record_property("fd_delta", delta)


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_selected_tree_opens_are_independent_of_unrelated_history(populated, monkeypatch, history, record_property):
    store, door, run, _rollup = populated
    with global_write_commit_scope(door), store._connection() as conn:
        conn.executemany(AGENT_RUN_UPSERT_SQL, (
            agent_run_values(AgentRunRecord(f"old-{index}", "other", status="DONE"), 1.0, 1.0)
            for index in range(history)))
        conn.commit()
    original, opened = sqlite3.connect, []

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    before = door.calls
    assert store.list_agent_tree("task").runs == [run]
    assert len(opened) == 1 and door.calls == before
    record_property("history_rows", history)
    record_property("passive_connects", len(opened))


# === test_r7_a_w3.py: 真实索引、canonical、只读 Store 与本机 HTTP 验证被动投影的范围和未知状态。 ===

@pytest.fixture
def case(tmp_path):
    root = tmp_path / "home"
    owner = ensure_owner_home(root, OwnerIdentity.local_main())
    paths = home_paths_with_owner(home_paths(root), owner)
    project = owner.home_dir / "project"
    project.mkdir()
    runtime = runtime_paths_for_agent(SimpleNamespace(), project, paths)
    local, door = make_store(runtime["local_store_path"].parent, enable_fts=False)
    store = ConversationStore(owner.data_dir / "conversations")
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "session-live",
        "channel_user_id": "local-agent", "owner_id": owner.owner_id, "owner_home": str(owner.home_dir),
        "workspace_root": str(project), "runtime_workspace_roots": [str(project)], "now": 1.0})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-live", "goal": "核对当前工作", "now": 2.0})
    manager = SubAgentManager(runtime["subagent_workspace"], local_store=local, conversation_store=store,
        workspace_root=project, owner_id=owner.owner_id, owner_home_dir=str(owner.home_dir),
        execution_mode="LOCAL_UNMANAGED")
    with global_write_commit_scope(door):
        child = manager.create_run(goal="直属工作", parent_id="task-live", root_id="task-live", depth=1,
            owner=owner.owner_id, attributes={"conversation_thread_id": thread.thread_id})
        grandchild = manager.create_run(goal="下级工作", parent_id=child.id, root_id="task-live", depth=2,
            owner=owner.owner_id, attributes={"conversation_thread_id": thread.thread_id})
    config = AgentConfig(my_agent_home=str(root), model_backend="echo", enable_tools=False)
    agent = SimpleNamespace(config=config, root=project, home_paths=paths, conversation_store=store,
        subagents=manager, subagent_run_ids_for_request=lambda key: SubagentRunIndex(
            manager.workspace, read_only=True).request_ids((key,), "", None))
    context = SimpleNamespace(owner_home=owner, identity=owner.identity)
    with closing(sqlite3.connect(local.db_path)) as keeper:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        yield SimpleNamespace(owner=owner, paths=paths, local=local, door=door, store=store, thread=thread,
            manager=manager, agent=agent, child=child, grandchild=grandchild, context=context, keeper=keeper)


def _observe(case, monkeypatch):
    scans, reads, copies = [], [], []
    original_load, original_copy = SubAgentPersistenceService.load, copy.deepcopy
    original_report = SubAgentPersistenceService.list_runs_report

    def full_report(service):
        scans.append(True)
        return original_report(service)

    def load(service, run_id):
        reads.append(run_id)
        return original_load(service, run_id)

    def clone(value, memo=None):
        if hasattr(value, "id"):
            copies.append(value.id)
        return original_copy(value, memo)

    monkeypatch.setattr(SubAgentPersistenceService, "list_runs_report", full_report)
    monkeypatch.setattr(SubAgentPersistenceService, "load", load)
    monkeypatch.setattr(copy, "deepcopy", clone)
    return scans, reads, copies


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_real_notice_and_status_wire_only_read_selected_ids(case, monkeypatch, record_property, history):
    index = case.manager.persistence.run_index
    with index.connect() as db:
        for number in range(history):
            task = _task(case.manager, f"history-{number}", "DONE", root="other-root", thread="other-thread")
            index.upsert(task, connection=db)
    scans, reads, copies = _observe(case, monkeypatch)
    calls = case.door.calls
    server = GatewayHTTPServer(0, gateway_paths_from_root(case.paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=case.agent))
    server.start()
    try:
        client_config = AgentConfig(my_agent_home=str(case.paths.root), gateway_port=server.server.server_address[1])
        client = GatewayChatClientAgent(None, client_config, case.agent.root, [case.agent.root], case.paths)
        notice = client.request_background_notices("session-live", after=0)
        assert notice["binding_state"] == "confirmed" and notice["snapshot_ok"] is True, notice
        activity = notice["agent_activity"]
        assert activity["subagent_projection_ok"] is True
        assert [row["run_id"] for row in activity["subagents"]] == [case.child.id]
        counts = control_service._subagent_status(case.agent, ["task-live"])
        assert (counts.total, counts.running) == (2, 2)
        # 同一真实客户端发送 /status，保留原控制结果信封及客户端 JSON 解析。
        status, body = client.post_gateway_json("/control", {"command": "/status", "conversation_id": "session-live",
            "metadata": {"message_id": "status-wire"}}, timeout=10)
        assert status == 200 and body["ok"] is True, body
        assert body["task_status"]["subagent_total"] == 2, body
        record_property("healthy_canonical_load_attempts", len(reads))
        record_property("healthy_canonical_copies", len(copies))
        record_property("healthy_history_scans", len(scans))
        assert set(reads) <= {"task-live", case.child.id, case.grandchild.id} and not scans
        assert set(copies) <= {case.child.id, case.grandchild.id}
        assert case.door.calls == calls
        # 单一真实缺库故障；已有连接关闭后删源，不能从历史文件补造完整投影。
        case.keeper.close()
        case.local.db_path.unlink()
        reads.clear()
        broken = client.request_background_notices("session-live", after=0)
        assert broken["binding_state"] == "confirmed" and broken["agent_activity"]["subagent_projection_ok"] is False
        assert "subagent_runs_unavailable" in broken["agent_activity"]["subagent_projection_warnings"]
        assert set(reads) <= {"task-live"} and not scans and case.door.calls == calls
        assert not case.local.db_path.exists()
        record_property("failed_child_load_attempts", len([run_id for run_id in reads if run_id != "task-live"]))
        record_property("python", sys.version)
        record_property("gil_enabled", getattr(sys, "_is_gil_enabled", lambda: True)())
    finally:
        server.stop()


def test_25_passive_readers_use_real_tree_and_release_descriptors(case, monkeypatch, record_property):
    scans, reads, _copies = _observe(case, monkeypatch)
    ready, release = threading.Event(), threading.Event()
    entered, lock = [], threading.Lock()
    selected = case.manager.list_runs_by_ids_report

    def wait_for_release(run_ids):
        with lock:
            entered.append(tuple(run_ids))
            if len(entered) == 25:
                ready.set()
        assert release.wait(10)
        return selected(run_ids)

    monkeypatch.setattr(case.manager, "list_runs_by_ids_report", wait_for_release)
    directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    # SQLite 会在仍有 keeper 时暂存并发读者的主库 FD；测量边界须包含 keeper 的实际关闭。
    case.keeper.close()
    before = len(os.listdir(directory)) if os.path.isdir(directory) else None
    calls = case.door.calls
    with closing(sqlite3.connect(case.local.db_path)) as keeper, ThreadPoolExecutor(max_workers=25) as workers:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        futures = [workers.submit(conversation_agent_activity, case.agent, case.store, case.thread.thread_id) for _ in range(25)]
        try:
            assert ready.wait(10)
            assert entered == [(case.child.id,)] * 25
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert all(result.subagent_projection_ok and result.subagents[0]["run_id"] == case.child.id for result in results)
    assert not scans and set(reads) <= {"task-live", case.child.id} and case.door.calls == calls
    if before is not None:
        assert len(os.listdir(directory)) - before == 0
    record_property("simultaneous_readers", len(results))
    record_property("selected_ids_per_reader", 1)
    record_property("fd_delta", 0 if before is not None else "unavailable")


@pytest.mark.parametrize("failure", ["pending", "corrupt", "missing-canonical"])
def test_status_and_closeout_keep_index_failure_unknown(case, monkeypatch, failure):
    scans, _reads, _copies = _observe(case, monkeypatch)
    index = case.manager.persistence.run_index
    if failure == "pending":
        with index.connect() as db:
            db.execute("UPDATE runs SET pending=1 WHERE run_id=?", (case.child.id,))
    elif failure == "corrupt":
        index.path.write_bytes(b"unavailable index")
    else:
        Path(case.child.agent_run_workspace_dir, "canonical_state.json").unlink()
    with pytest.raises(DataCorruptionError):
        control_service._subagent_status(case.agent, ["task-live"])
    params = _tool_loop_params(root_user_prompt="汇总当前工作", executed_tools=["create_subagents", "dispatch_subagents"])
    params_task_agent = SimpleNamespace(subagents=case.manager,
        subagent_run_ids_for_request=lambda _key: case.agent.subagent_run_ids_for_request("task-live"))
    facts = _delegated_work_facts(params_task_agent, params)
    if failure == "missing-canonical":
        assert facts.get("coverage") == "partial" and facts.get("load_errors")
    else:
        assert facts == {}
    assert not scans
    server = GatewayHTTPServer(0, gateway_paths_from_root(case.paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=case.agent))
    server.start()
    try:
        config = AgentConfig(my_agent_home=str(case.paths.root), gateway_port=server.server.server_address[1])
        client = GatewayChatClientAgent(None, config, case.agent.root, [case.agent.root], case.paths)
        status, body = client.post_gateway_json("/control", {"command": "/status", "conversation_id": "session-live",
            "metadata": {"message_id": "unavailable-status-wire"}}, timeout=10)
        assert status == 202 and body["control_state"] == "terminal_unknown", body
        assert body["ok"] is False and body["delivery_status"] == "unknown"
        assert body["error_code"] == "CONTROL_OPERATION_DELIVERY_UNKNOWN"
        assert "task_status" not in body and not scans
    finally:
        server.stop()


def test_cold_plugin_scope_is_indexed_existing_only_and_exact_get_never_discovers(case, monkeypatch):
    scans, reads, _copies = _observe(case, monkeypatch)

    def snapshot_files():
        snapshot = {}
        for directory, _subdirectories, filenames in os.walk(case.owner.home_dir):
            for filename in filenames:
                path = Path(directory) / filename
                if path.is_file():
                    info = path.stat()
                    snapshot[str(path.relative_to(case.owner.home_dir))] = (info.st_size, info.st_mtime_ns)
        return snapshot

    before = snapshot_files()
    calls = case.door.calls

    def forbidden(*args, **kwargs):
        pytest.fail("cold scoped read started a writer, maintenance thread, or workspace discovery")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(SubAgentPersistenceService, "_start_run_index_maintenance", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    payload = {"thread_id": case.thread.thread_id, "root_task_id": "task-live"}
    rows = plugin.list_subagents(case.context, payload, store=case.store)["items"]
    assert {row["run_id"] for row in rows} == {case.child.id, case.grandchild.id}
    detail = plugin.get_subagent(case.context, {**payload, "subagent_id": case.child.id}, store=case.store)
    assert detail["revision"] == case.manager.load(case.child.id).state_revision
    assert detail["thread_id"] == case.thread.thread_id
    for extra in ({"thread_id": "other-thread"}, {"root_task_id": "other-root"}):
        with pytest.raises(HostAPIError) as error:
            plugin.get_subagent(case.context, {**payload, **extra, "subagent_id": case.child.id})
        assert error.value.code == "SUBAGENT_NOT_FOUND"
    assert not scans and set(reads) <= {case.child.id, case.grandchild.id} and case.door.calls == calls
    after = snapshot_files()
    assert before == after


def test_plugin_terminal_roster_and_cross_owner_stay_canonical(case):
    with global_write_commit_scope(case.door):
        case.manager.mutate(case.child.id, lambda task: setattr(task, "status", "DONE"))
    payload = {"thread_id": case.thread.thread_id, "root_task_id": "task-live"}
    full = plugin.list_subagents(case.context, payload, store=case.store)["items"]
    active = plugin.list_subagents(case.context, {**payload, "include_finished": False}, store=case.store)["items"]
    assert {row["run_id"] for row in full} == {case.child.id, case.grandchild.id}
    assert [row["run_id"] for row in active] == [case.grandchild.id]
    other = ensure_owner_home(case.paths.root, OwnerIdentity.local_user("other"))
    context = SimpleNamespace(owner_home=other, identity=other.identity)
    with pytest.raises(HostAPIError) as error:
        plugin.get_subagent(context, {"subagent_id": case.child.id})
    assert error.value.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.parametrize("damage", ["missing", "partial", "pending", "missing-workspace"])
def test_cold_plugin_missing_coverage_is_unknown_without_writes(case, monkeypatch, damage):
    index = case.manager.persistence.run_index
    if damage == "missing":
        index.path.unlink()
    elif damage == "missing-workspace":
        case.manager.workspace.rename(case.manager.workspace.with_name("unavailable-subagents"))
    else:
        with index.connect() as db:
            db.execute("UPDATE meta SET value=0 WHERE key='complete'" if damage == "partial" else "UPDATE runs SET pending=1")
    folder = case.manager.workspace if case.manager.workspace.exists() else case.manager.workspace.parent
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("passive query tried to create or repair an index")

    monkeypatch.setattr(SubAgentPersistenceService, "_start_run_index_maintenance", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    with pytest.raises(HostAPIError) as error:
        plugin.list_subagents(case.context, {"thread_id": case.thread.thread_id}, store=case.store)
    assert error.value.code == "STORE_UNAVAILABLE" and error.value.status == 503
    after = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.iterdir()}
    assert before == after


# === test_r7_a_w4.py: 当前义务、精确 Audit 请求和启动终态窗口的真实索引行为回归。 ===

def _run(manager, run_id, status="RUNNING", *, thread="thread", root="root", capability="", **fields):
    task = _task(manager, run_id, status, thread=thread, root=root, capability=capability)
    if fields:
        path = manager.workspace / run_id / "task.json"
        row = json.loads(path.read_text())
        row.update(fields)
        path.write_text(json.dumps(row))
        task = manager.load(run_id)
    manager.persistence.run_index.upsert(task)
    return task


def _host(tmp_path, manager):
    store = ConversationStore(tmp_path / "conversations")
    thread = replace(store.threads.create(canonical_user_id="user"), task_ids=("root",))
    return SimpleNamespace(subagents=manager, conversation_store=store, scheduler_repository=None,
                           home_paths=SimpleNamespace(owner_home_dir=tmp_path)), thread


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_hot_read_counts_depend_on_current_obligations_not_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    with manager.persistence.run_index.connect() as db:
        for number in range(history):
            task = _task(manager, f"old-{number}", "DONE", root="elsewhere", thread="elsewhere")
            manager.persistence.run_index.upsert(task, connection=db)
    _run(manager, "live", thread=thread.thread_id, attributes={
        "conversation_thread_id": thread.thread_id, "allowed_skills": ["owner:live"]})
    reads, original = [], manager.persistence._cached_run_ref
    original_glob = Path.glob

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    def observed_glob(path, pattern, *args, **kwargs):
        assert path != manager.workspace or pattern != "*/task.json", "hot read enumerated historical runs"
        return original_glob(path, pattern, *args, **kwargs)

    # 库存准备均已完成；此作用域只观察热读，不在glob接缝替换后调用rglob。
    monkeypatch.setattr(Path, "glob", observed_glob)
    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *a, **k: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == "subagent_active"
    record_property("idle_canonical_reads", len(reads))
    record_property("idle_historical_reads", sum(run_id.startswith("old-") for run_id in reads))
    assert set(reads) == {"live"}
    assert "owner:live" in learning_protection_for_agent(host)["protected_ids"]
    assert watch._watch_dispatch_input_state(host).has_inputs
    assert watch._watch_scoped_dispatch_params(host, DispatchParams(limit=1)).include_run_ids == ["live"]
    assert [task.id for task in source._worker_runs_snapshot(host, audit_ids=("root",))] == ["live"]
    assert source.audit_task_has_unreported_findings(host, "root") is False
    startup._detect_active_tasks(host, startup.ActiveWorkSummary())
    startup._detect_crashed_running_tasks(host, startup.ActiveWorkSummary())
    assert set(reads) == {"live"} and len(reads) <= 12
    record_property("canonical_reads", len(reads))
    record_property("historical_reads", sum(run_id.startswith("old-") for run_id in reads))


@pytest.mark.parametrize("status,capability,bound,expected", [
    ("BLOCKED", "", True, "subagent_blocked"),
    ("DONE", "OPEN", True, "pending_approval"),
    ("DONE", "OPEN", False, "pending_approval"),
    ("BLOCKED", "", False, "subagent_blocked"),
])
def test_idle_preserves_terminal_and_unbound_obligations(tmp_path, status, capability, bound, expected):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "obligation", status, root="unrelated-root",
         thread=thread.thread_id if bound else "", capability=capability)
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == expected


@pytest.mark.parametrize("status,capability", [("BLOCKED", ""), ("TIMEOUT", ""), ("DONE", "DENIED")])
def test_identity_keeps_original_status_and_parent_resolution_predicates(tmp_path, status, capability):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "obligation", status, thread=thread.thread_id, capability=capability)
    refs, _ = identity_outstanding(host, thread)
    assert ("request-obligation" if capability else "obligation") in refs


@pytest.mark.parametrize("reader", ["idle", "watch", "identity", "startup", "findings"])
def test_single_partial_index_preserves_unknown_before_normal_convergence(tmp_path, reader):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "live", thread=thread.thread_id)
    with manager.persistence.run_index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    if reader == "idle":
        assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == "activity_unknown"
    elif reader == "watch":
        state = watch._watch_dispatch_input_state(host)
        assert state.load_error and not state.has_inputs
    elif reader == "identity":
        assert "subagents:unreadable" in identity_outstanding(host, thread)[0]
    elif reader == "startup":
        summary = startup.ActiveWorkSummary()
        startup._detect_crashed_running_tasks(host, summary)
        assert summary.detection_errors
    else:
        assert source.audit_task_has_unreported_findings(host, "root") is True


def test_healthy_idle_does_not_hydrate_foreign_terminal_rows(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "foreign", "DONE", root="foreign", thread="foreign")
    reads, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *a, **k: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == ""
    assert not watch._watch_dispatch_input_state(host).has_inputs
    assert reads == []


def test_terminal_audit_findings_and_quota_workers_are_exact_candidates(tmp_path):
    manager = _selected_manager(tmp_path)
    attrs = {AUDIT_SOURCE_WORKER_ATTR: True, CONVERSATION_REQUEST_ID_ATTR: "audit",
             AUDIT_SOURCE_ID_ATTR: "source", AUDIT_SOURCE_WATCH_ID_ATTR: "watch",
             AUDIT_SOURCE_OWNER_HOME_ATTR: str(tmp_path),
             AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key("audit", "watch")}
    ledger = tmp_path / "findings.jsonl"
    ledger.write_text('{"audit_finding": true, "requires_llm_report": true}\n')
    _run(manager, "terminal-finding", "DONE", attributes=attrs, agent_run_findings_jsonl=str(ledger))
    _run(manager, "quota", "BLOCKED", attributes=attrs, failure_type="provider_quota_exhausted")
    _run(manager, "foreign", "DONE", root="foreign", thread="foreign")
    host = SimpleNamespace(subagents=manager)
    assert source.audit_task_has_unreported_findings(host, "audit") is True
    assert {task.id for task in source._worker_runs_snapshot(host, audit_ids=("audit",))} == {"terminal-finding", "quota"}
    assert source.resume_quota_blocked_source_workers(host, "audit") == 1
    assert manager.load("quota").status == "PENDING"
    assert manager.load("terminal-finding").status == "DONE"


def test_startup_terminal_process_proof_uses_bounded_cursor_and_never_reopens(tmp_path, monkeypatch, record_property):
    from agent_py_agent.agent.subagents import process_control

    manager = _selected_manager(tmp_path)
    for number in range(RETENTION_SCAN_SLICE_ENTRIES + 1):
        _run(manager, f"old-{number:04}", "DONE", root="foreign", thread="foreign")
    terminal = _run(manager, "zz-orphan", "DONE", attributes={
        "background_start": {"pid": 424242, "launch_id": "launch", "attempt_id": "attempt", "status": "running"}})
    path = manager.workspace / terminal.id / "task.json"
    before = path.read_bytes()
    monkeypatch.setattr(process_control, "is_pid_alive", lambda pid: pid == 424242)
    monkeypatch.setattr(startup, "_process_cmdline", lambda pid: "python subagents-dispatch")
    reads, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    host = SimpleNamespace(subagents=manager)
    first = startup.ActiveWorkSummary()
    startup._detect_orphan_processes(host, first)
    assert getattr(first, "orphan_scan_partial", False) and len(reads) <= RETENTION_SCAN_SLICE_ENTRIES
    first_page_reads = len(reads)
    second = startup.ActiveWorkSummary()
    startup._detect_orphan_processes(host, second)
    assert second.orphan_scan_partial
    assert second.orphan_processes == [{"pid": 424242, "launch_id": "launch", "run_id": terminal.id,
        "task_status": "DONE", "cmdline_preview": "python subagents-dispatch"}]
    assert manager.load(terminal.id).status == "DONE" and path.read_bytes() == before
    record_property("first_page_reads", first_page_reads)


# === test_r7_a_w5.py: 派工只读当前 canonical 来源；索引不授予执行权，也不代替取消退出证明。 ===

def _agent(manager, tmp_path):
    return SimpleNamespace(subagents=manager, root=tmp_path, config=AgentConfig(), capability_router=CapabilityRouter(),
        _current_run_params=SimpleNamespace(request_id="request", run_id="request", task_id="request", task_attributes={}),
        subagent_run_ids_for_request=lambda _: [f"child-{number}" for number in range(7)])


def _context(**scope):
    return _dispatch_context_from_params(CapabilityRouter(), CapabilityConfig(), DispatchParams(**scope))


def _batch():
    return RunnerBatchContext([], 0, 0, "", 0, True, [])


def _indexed(manager, tasks):
    with manager.persistence.run_index.connect() as db:
        for task in tasks:
            manager.persistence.run_index.upsert(task, connection=db)


def _no_history(manager, monkeypatch):
    reads, stats = [], []
    load, stat, glob = manager.persistence.load, Path.stat, Path.glob

    def observed_load(run_id):
        reads.append(run_id)
        return load(run_id)

    def observed_stat(path, *args, **kwargs):
        if path.name in {"task.json", "canonical_state.json"}:
            stats.append(path.parent.name)
        return stat(path, *args, **kwargs)

    def observed_glob(path, pattern, **kwargs):
        assert path != manager.workspace or pattern != "*/task.json", "enumerated historical bodies"
        return glob(path, pattern, **kwargs)

    monkeypatch.setattr(manager.persistence, "load", observed_load)
    monkeypatch.setattr(Path, "stat", observed_stat)
    monkeypatch.setattr(Path, "glob", observed_glob)
    return reads, stats


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_selected_dispatch_completion_and_cleanup_ignore_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    selected = {f"child-{number}" for number in range(7)}
    _indexed(manager, (_task(manager, run_id, root="request", parent="request", thread="") for run_id in sorted(selected)))
    _indexed(manager, (_task(manager, f"old-{number}", "DONE", root="other") for number in range(history)))
    agent, ctx = _agent(manager, tmp_path), _context(root_id="request")
    reads, stats = _no_history(manager, monkeypatch)
    results = {}
    for name, call in {
        "completion": lambda: _delegated_work_facts(agent, _tool_loop_params(task_id="request")),
        "runner": lambda: execute_runner_jobs(agent, ctx, _batch()),
        "finalize": lambda: _finalize_scoped_tasks(agent, DispatchParams(root_id="request")),
        "pending": lambda: update_pending_work_state(agent, ctx=ctx),
        "visible": lambda: [task.id for task in dispatch_scope_tasks(agent, limit=20)],
        "hint": lambda: _known_children_hint(agent),
        "capability": lambda: make_capability_route_records(agent, ctx),
        "cancel_selection": lambda: _resolve_run_ids(agent, {"root_id": "request"}),
    }.items():
        reads.clear()
        stats.clear()
        results[name] = call()
        assert set(reads) <= selected and set(stats) <= selected, name
        assert len(reads) <= 14 and len(stats) <= 100, (name, reads, stats)
        record_property(name + "_loads", len(reads))
        record_property(name + "_stats", len(stats))
    assert results["completion"]["total"] == 7
    assert {task.id for task in results["finalize"]} == selected
    assert set(results["visible"]) == set(results["cancel_selection"].run_ids) == selected
    assert results["pending"] is False
    assert results["runner"] == results["capability"] == [] and all(run_id in results["hint"] for run_id in selected)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_completion_retains_partial_topology_and_read_errors(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    tasks = [_task(manager, f"child-{number}", root="request", parent="request" if number < 6 else "child-0")
             for number in range(7)]
    _indexed(manager, tasks)
    complete = _delegated_work_facts(_agent(manager, tmp_path), _tool_loop_params(task_id="request"))
    assert complete["total"] == 7 and complete["topology"]["descendant_count"] == 1 and "coverage" not in complete
    path = manager.workspace / tasks[-1].id / "task.json"
    path.unlink() if damage == "missing" else path.write_text("{broken")
    _no_history(manager, monkeypatch)
    facts = _delegated_work_facts(_agent(manager, tmp_path), _tool_loop_params(task_id="request"))
    assert facts["total"] == 6 and facts["status_counts"] == {"RUNNING": 6}
    assert facts["coverage"] == "partial" and facts["total_is_minimum"] is True
    assert facts["load_errors"][0]["run_id"] == "child-6"
    assert facts["topology"]["direct_child_count"] == 6


def test_retry_capability_and_terminal_patch_obligations_keep_original_gates(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    tasks = [_task(manager, status.lower(), status, root="request", parent="request", thread="")
             for status in ("FAILED", "TIMEOUT", "BLOCKED", "RUNNING", "DONE")]
    for task in tasks[:3]:
        task.failure_type = "runner_error"
        manager.save(task)
    patch = tasks[-1]
    patch.output_json = str(manager.workspace / patch.id / "output.json")
    manager.save(patch)
    Path(manager.load(patch.id).output_json).write_text(json.dumps({"patches": [{"path": "result.txt"}]}))
    tasks.append(_task(manager, "approval", "DONE", root="request", parent="request", capability="OPEN"))
    _indexed(manager, tasks)
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    selected = dispatch_scope_tasks(agent)
    policy = RunnerCandidatePolicy(runner_retry_limit=1)
    assert {task.id for task in selected if _is_dispatch_runner_candidate(task, policy=policy)} == {"failed", "timeout", "blocked"}
    assert not any(_is_dispatch_runner_candidate(task, policy=RunnerCandidatePolicy()) for task in selected)
    assert "approval(DONE,未决申请x1)" in _known_children_hint(agent)
    ctx = _context(root_id="request", max_runners=7)
    records = execute_runner_jobs(agent, ctx, _batch())
    assert {record.run_id for record in records} == {"failed", "timeout", "blocked"}
    assert all(record.dry_run and not record.applied for record in records)
    assert update_pending_work_state(agent, ctx=ctx) is True


def test_explicit_include_miss_exclusion_and_suffix_diagnostics(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "worker-actual-123", "PLANNING", root="request"),
                       _task(manager, "excluded", "PLANNING", root="request"),
                       _task(manager, "foreign", "PLANNING", root="other")])
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    ctx = _context(root_id="request", include_run_ids=["worker-wrong-123", "excluded", "foreign"], exclude_run_ids=["excluded"])
    records = execute_runner_jobs(agent, ctx, _batch())
    assert len(records) == 1 and records[0].action == "invalid_run_ids" and not records[0].ok
    assert "worker-actual-123" in records[0].message and "possible_corrections" in records[0].message
    assert "valid_scope_run_ids=['worker-actual-123']" in records[0].message
    assert "excluded" in records[0].message and "foreign" in records[0].message


@pytest.mark.parametrize("damage", ["missing", "partial", "corrupt"])
def test_unknown_index_is_neither_empty_success_nor_history_fallback(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "one", root="request")])
    manager.persistence.read_only = True  # 只读消费方不启动维护；UNKNOWN 必须原样报告。
    index = manager.persistence.run_index
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not sqlite")
    else:
        with index.connect() as db:
            db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    agent = _agent(manager, tmp_path)
    _no_history(manager, monkeypatch)
    errors = (SubagentRunIndexUnavailable, FileNotFoundError, sqlite3.DatabaseError)
    with pytest.raises(errors):
        dispatch_scope_tasks(agent)
    with pytest.raises(errors):
        execute_runner_jobs(agent, _context(root_id="request"), _batch())
    selection = _resolve_run_ids(agent, {"root_id": "request"})
    assert not selection.ok and selection.error_payload["error"]


def test_display_limits_apply_before_canonical_body_reads(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, (_task(manager, f"child-{number}", root="request", parent="request") for number in range(40)))
    _indexed(manager, [_task(manager, "unrelated", root="elsewhere", parent="elsewhere")])
    agent = _agent(manager, tmp_path)
    reads, _stats = _no_history(manager, monkeypatch)
    ids = [task.id for task in dispatch_scope_tasks(agent, limit=20)]
    assert len(ids) == len(reads) == 20 and "unrelated" not in reads
    reads.clear()
    hint = _known_children_hint(agent)
    assert len(reads) <= 12 and hint.count("(RUNNING") == 12 and "unrelated" not in reads


def test_cancel_subtree_selection_includes_deep_children_without_siblings(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "root", root="root"), _task(manager, "branch", root="root", parent="root"),
                       _task(manager, "child", root="root", parent="branch"),
                       _task(manager, "deep", "DONE", root="root", parent="child"),
                       _task(manager, "sibling", root="root", parent="root"), _task(manager, "old", "DONE", root="other")])
    agent = _agent(manager, tmp_path)
    reads, _stats = _no_history(manager, monkeypatch)
    selection = _resolve_run_ids(agent, {"root_id": "branch"})
    assert selection.ok and set(selection.run_ids) == {"branch", "child", "deep"}
    assert set(reads) <= {"branch", "child", "deep"}
    reads.clear()
    assert _resolve_run_ids(agent, {"run_ids": ["branch"]}).run_ids == ["branch"]
    assert set(reads) <= {"branch", "root"} and reads[0] == "branch"
    assert _resolve_run_ids(agent, {"run_id": "absent"}).error_payload["reason"] == "requested_run_id_not_in_visible_scope"


@pytest.mark.parametrize("selected", [True, False])
def test_planner_forwards_same_exact_candidates_to_all_board_methods(tmp_path, monkeypatch, selected):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.refs import candidate_ids_kwargs

    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "selected", root="request" if selected else "other"), _task(manager, "old", "DONE", root="other")])
    board, observed = manager.board, {}
    # W6 未集成时，仅在真实方法边界观察新 keyword；结果仍由原 board 计算。
    for name in ("build_board", "due_check", "plan_actions"):
        original = getattr(board, name)

        def supporting(*args, candidate_ids=None, _method=original, _name=name, **kwargs):
            observed.setdefault(_name, []).append(candidate_ids)
            return _method(*args, **kwargs, **candidate_ids_kwargs(_method, candidate_ids))

        monkeypatch.setattr(board, name, supporting)
    _board, ctx = _collect_parent_planner_context(_agent(manager, tmp_path), PlannerStateParams(CapabilityConfig(), 7, 20, "test", ""))
    expected = ["selected"] if selected else []
    assert all(observed[name][0] == expected for name in ("build_board", "due_check", "plan_actions"))
    assert [task.id for task in ctx.tasks] == expected


def test_legacy_board_signature_and_explicit_history_remain_available(tmp_path):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "selected", root="request"), _task(manager, "old", "DONE", root="other")])
    _board, ctx = _collect_parent_planner_context(_agent(manager, tmp_path), PlannerStateParams(CapabilityConfig(), 7, 20, "test", ""))
    assert [task.id for task in ctx.tasks] == ["selected"]
    historical = SimpleNamespace(subagents=manager)
    assert {task.id for task in dispatch_scope_tasks(historical)} == {"selected", "old"}


def test_display_roster_uses_direct_parent_before_limit_and_keeps_terminal_approval(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "z-child", root="request", parent="request"),
                       _task(manager, "z-approval", "DONE", root="request", parent="request", capability="OPEN")])
    _indexed(manager, (_task(manager, f"a-grandchild-{number}", root="request", parent="z-child") for number in range(40)))
    reads, _stats = _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    assert {task.id for task in dispatch_scope_tasks(agent, limit=20)} == {"z-child", "z-approval"}
    hint = _known_children_hint(agent)
    assert "z-child(RUNNING" in hint and "z-approval(DONE,未决申请x1)" in hint
    assert set(reads) <= {"z-child", "z-approval"}


def test_cancel_cleanup_event_preserves_terminal_exit_and_other_scopes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.subagents import cancellation, cancellation_hosts
    from agent_py_agent.tests.test_orchestration_cancel_subagents_tool import (
        _create_cancel_tree,
        _execute_host_cancel_subagents,
        _fresh_runner_session,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    parent, child, history = _create_cancel_tree(agent)
    child.status, child.runner_active_attempt_id = "RUNNING", "original-attempt"
    session = _fresh_runner_session(worker_pid=5353, in_process=False)
    session.update(run_id=child.id, attempt_id="original-attempt", launch_id="exclusive", worker_birth_token="original-birth")
    child.attributes.update(runner_session=session, background_start={"pid": 5353, "attempt_id": "original-attempt", "launch_id": "exclusive"})
    agent.subagents.save(child)
    stale = agent.subagents.load(child.id)
    history_path = Path(agent.subagents.load(history.id).task_dir) / "canonical_state.json"
    history_before = (history_path.read_bytes(), history_path.stat().st_mtime_ns)
    entered, release = Event(), Event()
    cleanup = cancellation.cleanup_runner_stop

    def scheduled(stop):
        entered.set()
        assert release.wait(10)
        return cleanup(stop)

    monkeypatch.setattr(cancellation, "cleanup_runner_stop", scheduled)
    monkeypatch.setattr(cancellation_hosts, "is_pid_alive", lambda _pid: False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_execute_host_cancel_subagents, agent, {"root_id": child.id, "reason": "关闭原执行轮"})
        try:
            assert entered.wait(10)
            stale.status = "PENDING"
            agent.subagents.save(stale)
            current = agent.subagents.load(child.id)
            assert current.status == "CANCELLED" and not current.runner_active_attempt_id
            # 取消先使canonical终态单调；锁外cleanup尚未返回，不得伪签runner退出。
            assert current.attributes["runner_session"]["status"] == "running"
            assert current.attributes["runner_session"].get("ended_at", 0) == 0
            assert not _is_dispatch_runner_candidate(current, policy=RunnerCandidatePolicy(runner_retry_limit=1))
        finally:
            release.set()
        result = future.result(timeout=10)
    assert result.ok and json.loads(result.output)["cancelled"][0]["pid_report"]["host_exited"] is True
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.subagents.load(parent.id).status == "RUNNING"
    assert (history_path.read_bytes(), history_path.stat().st_mtime_ns) == history_before


def test_stale_index_does_not_grant_scope_or_dispatch_authority(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "child", "PLANNING", root="request", parent="request")])
    path = manager.workspace / "child" / "task.json"
    payload = json.loads(path.read_text())
    payload.update(root_id="foreign", parent_id="foreign")
    path.write_text(json.dumps(payload))
    _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    with pytest.raises(SubagentRunIndexUnavailable, match="canonical relation changed"):
        execute_runner_jobs(agent, _context(root_id="request"), _batch())
    assert json.loads(path.read_text())["status"] == "PLANNING"


def test_pending_count_preserves_excluded_and_explicit_status_selection(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "pending", "PLANNING", root="request"),
                       _task(manager, "done", "DONE", root="other")])
    _no_history(manager, monkeypatch)
    agent = _agent(manager, tmp_path)
    assert update_pending_work_state(agent, ctx=_context(root_id="request", exclude_run_ids=["pending"])) is False
    result = _resolve_run_ids(agent, {"status": "DONE"})
    assert result.ok and result.run_ids == ["done"]


def test_remembered_current_run_keeps_its_canonical_root_obligations(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "remembered", root="request", parent="request"),
                       _task(manager, "approval", "DONE", root="request", parent="request", capability="OPEN"),
                       _task(manager, "old", "DONE", root="other")])
    agent = SimpleNamespace(subagents=manager, _orchestration_run_ids_seen={"remembered"})
    reads, _stats = _no_history(manager, monkeypatch)
    assert {task.id for task in dispatch_scope_tasks(agent)} == {"remembered", "approval"}
    assert set(reads) <= {"remembered", "approval"}


def test_pending_index_converges_through_original_bounded_repair(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    _indexed(manager, [_task(manager, "one", root="request")])
    path = manager.workspace / "one" / "task.json"
    before = path.read_bytes()
    with manager.persistence.run_index.connect() as db:
        db.execute("UPDATE runs SET pending=1")
    _no_history(manager, monkeypatch)
    assert [task.id for task in dispatch_scope_tasks(_agent(manager, tmp_path))] == ["one"]
    assert manager.persistence.run_index.readiness()["pending"] == 0 and path.read_bytes() == before
