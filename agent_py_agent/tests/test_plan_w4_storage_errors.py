"""W4：真实锁竞争、连接退役、已执行未知边界和组件发送事实。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection, sqlite_error_details
from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationOwnershipError,
    ToolOperationReopenRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.recovery_guard import (
    RuntimeDatabaseDeferred,
    runtime_db_corruption_recovery_pending,
    runtime_db_generation_guard,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolHandlerOutcome,
    ToolOperationReconciliation,
)
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


class CountingTool(BaseTool):
    def __init__(self, *, effect="read_only", callback=None):
        self.model_spec = make_test_model_spec("w4_probe")
        self.runtime_policy = make_test_runtime_policy(effect)
        self.calls = 0
        self.callback = callback
        self.contexts = []

    def execute(self, params):
        self.calls += 1
        if self.callback is not None:
            return self.callback()
        return ToolHandlerOutcome("w4_probe", True, "done")

    def reconcile_operation(self, params, context):
        self.contexts.append(context)
        return ToolOperationReconciliation(reason="provider facts remain unknown")


def run_tool(root, store, tool, *, attempt="attempt-w4", call_id="call-w4"):
    return execute_canonical_test_call(
        root, tools={"w4_probe": tool}, tool_name="w4_probe", arguments={},
        run_id="run-w4", attempt_id=attempt, call_id=call_id,
        operation_id=f"tool_call:{attempt}:{call_id}",
        operation_store=store, operation_store_required=True, operation_owner_id="owner-w4",
        trusted_run_context={"run_scope": {"task_id": "task-w4"}},
    ).result


def managed_store(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(
        owner_id="owner-w4", goal="W4 probe", conversation_task_id="task-w4",
        thread_id="thread-w4", run_id="run-w4", role="assistant",
    )
    return repo, ManagedOperationStore(repo), str(chain["attempt_id"])


def storage_error(code):
    error = sqlite3.DatabaseError("injected storage fault")
    error.sqlite_errorcode = code
    error.sqlite_errorname = {5: "SQLITE_BUSY", 6: "SQLITE_LOCKED", 11: "SQLITE_CORRUPT", 26: "SQLITE_NOTADB"}.get(code, "SQLITE_BUSY_RECOVERY")
    return error


def test_authority_busy_retries_same_identity_after_real_thread_lock(tmp_path, monkeypatch):
    repo, store, attempt = managed_store(tmp_path)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    locked, saw_busy, released = threading.Event(), threading.Event(), threading.Event()
    failures, requests = [], []

    def writer():
        try:
            with sqlite3.connect(repo.db_path, timeout=0) as conn:
                conn.execute("BEGIN EXCLUSIVE")
                locked.set()
                assert saw_busy.wait(5)
                conn.rollback()
        except BaseException as exc:
            failures.append(exc)
        finally:
            released.set()

    @contextmanager
    def readonly_connection():
        def connect():
            conn = sqlite3.connect(f"file:{repo.db_path}?mode=ro", uri=True, timeout=0,
                                   factory=TrackedSQLiteConnection)
            conn.row_factory = sqlite3.Row
            return conn

        try:
            with scoped_connection(repo.db_path, True, connect) as conn:
                yield conn
        except sqlite3.Error as exc:
            assert exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            saw_busy.set()
            assert released.wait(5)
            raise

    original = store.require_authority

    def require(request, **kwargs):
        requests.append(request)
        return original(request, **kwargs)

    monkeypatch.setattr(repo, "_runtime_connection", readonly_connection)
    monkeypatch.setattr(store, "require_authority", require)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert locked.wait(5)
        tool = CountingTool()
        with sqlite_connection_scope(True):
            result = run_tool(tmp_path, store, tool, attempt=attempt)
    finally:
        saw_busy.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert result.ok, result.output
    assert tool.calls == 1
    assert len(requests) == 2 and requests[0] is requests[1]
    assert requests[0].attempt_id == attempt


@pytest.mark.parametrize("code,expected_reads", [(5, 3), (261, 3), (6, 1), (11, 1), (26, 1)])
def test_authority_retry_budget_and_structured_diagnostic(tmp_path, code, expected_reads):
    reads = []
    error = storage_error(code)

    class BrokenStore:
        def require_authority(self, request):
            reads.append(request)
            raise error

    tool = CountingTool()
    result = run_tool(tmp_path, BrokenStore(), tool)
    assert not result.ok and not result.handler_executed and tool.calls == 0
    assert len(reads) == expected_reads
    facts = result.metadata["handler_details"].get("storage_error", {})
    assert facts.get("sqlite_errorcode") == code
    assert facts.get("sqlite_primary_code") == code & 0xFF


def test_busy_text_without_structured_code_is_not_retried(tmp_path):
    reads = []

    class BrokenStore:
        def require_authority(self, request):
            reads.append(request)
            raise sqlite3.OperationalError("database is locked SQLITE_BUSY")

    tool = CountingTool()
    result = run_tool(tmp_path, BrokenStore(), tool)
    assert not result.ok and len(reads) == 1 and tool.calls == 0


# LLM: 691c5345d 起结构性错误发布持久恢复意图；新借用先被延后，经 EX 恢复者清除意图后才可重开。
# 函数用途: 断言结构性失败后的库在恢复前不再借出，恢复后新物理连接可用。
def _assert_fenced_until_exclusive_recovery(path, read_only, connect):
    with pytest.raises(RuntimeDatabaseDeferred, match="recovery_pending"):
        with scoped_connection(path, read_only, connect):
            pass
    with runtime_db_generation_guard(path, exclusive=True):
        pass


@pytest.mark.parametrize("caught_inside", [False, True])
@pytest.mark.parametrize("kind", ["statement", "corrupt", "notadb"])
def test_exceptional_connection_is_retired_even_if_sql_error_was_caught(tmp_path, caught_inside, kind):
    path = tmp_path / "scoped.db"
    opened = []

    def connect():
        conn = sqlite3.connect(path, factory=TrackedSQLiteConnection)
        opened.append(conn)
        return conn

    def fail(conn):
        if kind == "statement":
            conn.execute("SELECT * FROM missing_w4_table")
        raise storage_error(11 if kind == "corrupt" else 26)

    with sqlite_connection_scope(True):
        if caught_inside:
            with scoped_connection(path, True, connect) as conn:
                with pytest.raises(sqlite3.Error):
                    if kind == "statement":
                        fail(conn)
                    else:
                        conn.create_function("w4_fail", 0, lambda: (_ for _ in ()).throw(storage_error(26)))
                        conn.execute("SELECT w4_fail()")
        else:
            with pytest.raises(sqlite3.Error), scoped_connection(path, True, connect) as conn:
                fail(conn)
            if kind != "statement":
                _assert_fenced_until_exclusive_recovery(path, True, connect)
        with scoped_connection(path, True, connect) as fresh:
            assert fresh.execute("SELECT 1").fetchone()[0] == 1
        assert len(opened) == 2 and opened[0] is not opened[1]
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")


@pytest.mark.parametrize("code", [11, 26])
def test_structural_store_failure_stops_without_rebuilding_or_cleanup_write(tmp_path, monkeypatch, code):
    repo, store, attempt = managed_store(tmp_path)
    tool = CountingTool(effect="mutating")
    original = repo.db_path.read_bytes()
    reads = []

    @contextmanager
    def broken_connection():
        reads.append(1)
        raise storage_error(code)
        yield

    monkeypatch.setattr(repo, "_runtime_connection", broken_connection)
    result = run_tool(tmp_path, store, tool, attempt=attempt)
    assert not result.ok and tool.calls == 0 and len(reads) == 1
    assert repo.db_path.read_bytes() == original


@pytest.mark.parametrize("kind", ["local", "managed"])
def test_result_save_failure_preserves_started_identity_and_blocks_new_call(tmp_path, monkeypatch, kind):
    if kind == "managed":
        repo, store, attempt = managed_store(tmp_path)
    else:
        store, attempt = LocalStore(tmp_path / "local.db", enable_fts=False), "attempt-w4"
    tool = CountingTool(effect="mutating")

    def fail_save(request):
        raise storage_error(26)

    monkeypatch.setattr(store, "finish_tool_operation", fail_save)
    first = run_tool(tmp_path, store, tool, attempt=attempt)
    assert not first.ok and first.handler_executed and first.effect_outcome == "unknown"
    assert first.failure_stage == "persistence"
    facts = first.metadata["handler_details"]["tool_operation"]
    assert facts["operation_id"] == f"tool_call:{attempt}:call-w4"
    assert facts.get("attempt_id") == attempt
    assert facts.get("phase") != "not_started"
    assert first.metadata["handler_details"]["storage_error"]["sqlite_primary_code"] == 26
    assert not run_tool(tmp_path, store, tool, attempt=attempt).handler_executed
    assert not run_tool(tmp_path, store, tool, attempt=attempt, call_id="new-live-call").handler_executed
    # 领取行还在执行中也不能用新 call id 绕过；先模拟持有者过期，让原行显式转 UNKNOWN。
    if kind == "local":
        with store._connection() as conn:
            conn.execute("UPDATE tool_operations SET lease_expires_at=0")
            conn.commit()
    else:
        with repo._runtime_connection() as conn:
            conn.execute("UPDATE tool_operations SET outcome_json=json_set(outcome_json, '$.lease_expires_at', 0)")
            conn.commit()
    assert not run_tool(tmp_path, store, tool, attempt=attempt, call_id="new-call").handler_executed
    assert tool.calls == 1


@pytest.fixture(params=["local", "managed"])
def claimed_store(tmp_path, request):
    if request.param == "managed":
        _repo, store, attempt = managed_store(tmp_path)
    else:
        store, attempt = LocalStore(tmp_path / "local.db", enable_fts=False), "attempt-w4"
    holder = new_tool_operation_holder()
    claim_request = ToolOperationClaimRequest(
        owner_id="owner-w4", run_id="run-w4", task_id="task-w4", operation_id="operation-w4",
        tool="w4_probe", args_hash="args-w4", idempotency_key="key-w4",
        idempotency_scope="operation", idempotency_namespace="w4_probe",
        holder=holder, lease_expires_at=time.time() + 900, attempt_id=attempt,
    )
    return store, holder, store.claim_tool_operation(claim_request).record


def test_dispatch_checkpoint_survives_unknown_and_reopen_without_reset(claimed_store):
    store, holder, record = claimed_store
    assert getattr(record, "first_dispatch_at", "missing") is None
    assert getattr(record, "component_receipts", "missing") == []
    checkpoint = getattr(store, "record_tool_operation_dispatch", None)
    assert callable(checkpoint), "store must persist dispatch facts before sending"
    upload = checkpoint(record, component_id="file-1", receipt={"upload_ref": "upload:1"})
    assert upload.first_dispatch_at is None
    first = checkpoint(upload, component_id="file-1", first_dispatch_at=100.0,
                       receipt={"message_key": "file-key", "phase": "dispatching"})
    assert first.first_dispatch_at == 100.0
    second = checkpoint(first, component_id="text-1", first_dispatch_at=200.0,
                        receipt={"message_key": "text-key"})
    unknown = store.finish_tool_operation(ToolOperationCompletionRequest(
        owner_id=second.owner_id, run_id=second.run_id, operation_id=second.operation_id,
        holder_id=second.holder_id, generation=second.generation, status="unknown",
        result={"provider_facts": "unknown"},
    ))
    reopened = store.reopen_tool_operation_after_reconciliation(ToolOperationReopenRequest(
        owner_id=unknown.owner_id, run_id=unknown.run_id, operation_id=unknown.operation_id,
        expected_generation=unknown.generation, holder=holder, lease_expires_at=time.time() + 900,
        source_ref="provider:same-key-native-window",
    ))
    repeated = checkpoint(reopened, component_id="file-1", first_dispatch_at=300.0,
                          receipt={"provider_message_id": "message:1", "phase": "succeeded"})
    assert repeated.first_dispatch_at == 100.0
    assert repeated.component_receipts == [
        {"component_id": "file-1", "upload_ref": "upload:1", "message_key": "file-key",
         "first_dispatch_at": 100.0, "provider_message_id": "message:1", "phase": "succeeded"},
        {"component_id": "text-1", "message_key": "text-key", "first_dispatch_at": 200.0},
    ]
    saved = store.get_tool_operation(owner_id=record.owner_id, run_id=record.run_id,
                                     operation_id=record.operation_id)
    assert saved.first_dispatch_at == 100.0 and saved.component_receipts == repeated.component_receipts
    with pytest.raises(ToolOperationOwnershipError) as caught:
        checkpoint(record, component_id="file-1", first_dispatch_at=400.0)
    assert "ownership" in str(caught.value).lower() or "holder" in str(caught.value).lower()


def test_legacy_reconciliation_keeps_unknown_dispatch_time(claimed_store):
    _store, _holder, record = claimed_store
    assert getattr(record, "first_dispatch_at", "missing") is None
    assert getattr(record, "component_receipts", "missing") == []
    assert getattr(record, "attempt_id", "")


def test_handler_checkpoint_is_durable_before_send_and_reaches_reconciler(tmp_path):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    checkpoint = getattr(coordinator, "record_tool_operation_dispatch", None)
    assert callable(checkpoint), "delivery leaves need an original-claim checkpoint"
    receipts = []

    def send():
        upload = checkpoint("file-1", receipt={"upload_ref": "upload:1"}, dispatch_started=False)
        assert upload.first_dispatch_at is None
        dispatched = checkpoint("file-1", receipt={"message_key": "stable-key", "phase": "dispatching"})
        durable = store.get_tool_operation(owner_id="owner-w4", run_id="run-w4",
                                           operation_id=dispatched.operation_id)
        assert durable.first_dispatch_at == dispatched.first_dispatch_at
        receipts.extend(durable.component_receipts)
        return ToolHandlerOutcome("w4_probe", False, "provider unknown", error_code="TOOL_TIMEOUT")

    tool = CountingTool(effect="mutating", callback=send)
    first = run_tool(tmp_path, store, tool)
    assert first.effect_outcome == "unknown" and tool.calls == 1
    second = run_tool(tmp_path, store, tool)
    assert not second.handler_executed and tool.calls == 1 and len(tool.contexts) == 1
    context = tool.contexts[0]
    assert context.prior_result["first_dispatch_at"] == receipts[0]["first_dispatch_at"]
    assert context.prior_result["component_receipts"] == receipts
    assert context.prior_result["attempt_id"] == "attempt-w4"
    with pytest.raises(ToolWriteContextUnavailable):
        checkpoint("too-late")


def test_local_database_and_live_sidecars_are_private_under_permissive_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        store = LocalStore(tmp_path / "private.db", enable_fts=False)
        with store._connection() as conn:
            conn.execute("INSERT INTO metadata(key,value) VALUES('private','yes')")
            paths = [store.db_path, store.db_path.with_name("private.db-wal"),
                     store.db_path.with_name("private.db-shm")]
            assert all(path.exists() for path in paths)
            assert [path.stat().st_mode & 0o777 for path in paths] == [0o600] * 3
    finally:
        os.umask(previous)


def test_unused_tracked_connection_is_clean_and_reusable(tmp_path):
    path, opened = tmp_path / "unused.db", []

    def connect():
        conn = sqlite3.connect(path, factory=TrackedSQLiteConnection)
        opened.append(conn)
        return conn

    with sqlite_connection_scope(True):
        with scoped_connection(path, False, connect):
            pass
        with scoped_connection(path, False, connect) as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert len(opened) == 1


def test_legacy_local_table_migrates_without_inventing_dispatch_time(tmp_path):
    path = tmp_path / "legacy.db"
    # 旧版真实表不含新增三列；保留已有 UNKNOWN 行，初始化只能增量迁移。
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE tool_operations (
            owner_id TEXT NOT NULL, run_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
            operation_id TEXT NOT NULL, tool TEXT NOT NULL, args_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, idempotency_scope TEXT NOT NULL,
            idempotency_namespace TEXT NOT NULL, status TEXT NOT NULL, holder_id TEXT NOT NULL,
            holder_host TEXT NOT NULL DEFAULT '', holder_pid INTEGER NOT NULL DEFAULT 0,
            holder_process_start_token TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 1,
            lease_expires_at REAL NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '', unknown_reason TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(owner_id, run_id, operation_id))""")
        conn.execute("""INSERT INTO tool_operations(owner_id,run_id,operation_id,tool,args_hash,
            idempotency_key,idempotency_scope,idempotency_namespace,status,holder_id,
            lease_expires_at,created_at,updated_at) VALUES
            ('owner-w4','run-w4','legacy-op','w4_probe','legacy-args','legacy-key','operation',
             'w4_probe','unknown','legacy-holder',0,1,2)""")
    store = LocalStore(path, enable_fts=False)
    record = store.get_tool_operation(owner_id="owner-w4", run_id="run-w4", operation_id="legacy-op")
    assert record.status == "unknown" and record.created_at == 1
    assert record.first_dispatch_at is None and record.component_receipts == [] and record.attempt_id == ""
    assert len(store.list_tool_operations(owner_id="owner-w4")) == 1


@pytest.mark.parametrize("body_failed", [False, True])
@pytest.mark.parametrize("code", [11, 26])
def test_structural_rollback_error_survives_cleanup_without_replacing_body(tmp_path, body_failed, code):
    path = tmp_path / "rollback.db"
    body, broken, opened = ValueError("business failure"), storage_error(code), []

    class LaterCloseStop(BaseException):
        pass

    class BrokenRollback(TrackedSQLiteConnection):
        def rollback(self):
            raise broken

        def close(self):
            super().close()
            raise LaterCloseStop("later close interruption")

    def connect():
        conn = sqlite3.connect(path, factory=BrokenRollback if not opened else TrackedSQLiteConnection)
        opened.append(conn)
        return conn

    with sqlite_connection_scope(True):
        with pytest.raises((ValueError, sqlite3.Error, LaterCloseStop)) as caught, scoped_connection(path, False, connect) as conn:
            conn.execute("CREATE TABLE rollback_probe(value)")
            conn.execute("INSERT INTO rollback_probe VALUES(1)")
            if body_failed:
                raise body
        assert caught.value is (body if body_failed else broken)
        diagnostic = sqlite_error_details(caught.value)
        assert diagnostic["category"] == "structural"
        if body_failed:
            assert diagnostic["cleanup_error"]["sqlite_primary_code"] == code
        else:
            assert diagnostic["sqlite_primary_code"] == code
        assert runtime_db_corruption_recovery_pending(path)
        # 只读不能消费错误意图；下一写口须先经EX证实这是注入的误报，而非永久跳过恢复。
        with pytest.raises(RuntimeDatabaseDeferred, match="recovery_pending"):
            with scoped_connection(path, True, connect):
                pytest.fail("只读请求不能清除损坏意图")
        identity = (path.stat().st_dev, path.stat().st_ino)
        assert len(opened) == 1
        with scoped_connection(path, False, connect) as fresh:
            assert fresh.execute("SELECT count(*) FROM rollback_probe").fetchone()[0] == 0
        recovery = json.loads(path.with_name(path.name + '.recovery-result.json').read_text())
        assert recovery['status'] == 'healthy'
        assert (path.stat().st_dev, path.stat().st_ino) == identity
        assert not runtime_db_corruption_recovery_pending(path)
        assert len(opened) == 2


@pytest.mark.parametrize("kind", ["local", "managed"])
def test_equivalent_live_operation_blocks_without_rebinding_new_identity(tmp_path, kind):
    if kind == "managed":
        _repo, store, attempt = managed_store(tmp_path)
    else:
        store, attempt = LocalStore(tmp_path / "local.db", enable_fts=False), "attempt-w4"
    entered, release, results = threading.Event(), threading.Event(), []

    def effect():
        entered.set()
        assert release.wait(5)
        return ToolHandlerOutcome("w4_probe", True, "done once")

    tool = CountingTool(effect="mutating", callback=effect)
    thread = threading.Thread(target=lambda: results.append(run_tool(tmp_path, store, tool, attempt=attempt)))
    thread.start()
    try:
        assert entered.wait(5)
        blocked = run_tool(tmp_path, store, tool, attempt=attempt, call_id="independent-call")
        assert not blocked.handler_executed and blocked.effect_outcome == "not_started"
        facts = blocked.metadata["handler_details"]["tool_operation"]
        assert facts["operation_id"] == f"tool_call:{attempt}:independent-call"
        assert facts["status"] == "not_started"
        assert blocked.metadata["handler_details"]["blocking_operation"]["operation_id"] == f"tool_call:{attempt}:call-w4"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(results) == 1 and results[0].ok and tool.calls == 1


@pytest.mark.parametrize("kind", ["local", "managed"])
def test_reconciliation_can_append_receipt_but_cannot_start_a_dispatch(tmp_path, kind):
    if kind == "managed":
        _repo, store, attempt = managed_store(tmp_path)
    else:
        store, attempt = LocalStore(tmp_path / "local.db", enable_fts=False), "attempt-w4"
    checkpoint = coordinator.record_tool_operation_dispatch

    def send():
        checkpoint("text-1", receipt={"message_key": "same-message-key"})
        return ToolHandlerOutcome("w4_probe", False, "provider unknown", error_code="TOOL_TIMEOUT")

    class ReconcilingTool(CountingTool):
        def reconcile_operation(self, params, context):
            self.contexts.append(context)
            # 核对只补提供方事实，不能给旧操作制造一次新的首次发送。
            with pytest.raises(ToolWriteContextUnavailable):
                checkpoint("new-component")
            checkpoint("text-1", receipt={"provider_message_id": "provider:known"}, dispatch_started=False)
            return ToolOperationReconciliation(reason="other components remain unknown")

    tool = ReconcilingTool(effect="mutating", callback=send)
    first = run_tool(tmp_path, store, tool, attempt=attempt)
    before = store.get_tool_operation(owner_id="owner-w4", run_id="run-w4", operation_id=first.operation.operation_id)
    second = run_tool(tmp_path, store, tool, attempt=attempt)
    after = store.get_tool_operation(owner_id="owner-w4", run_id="run-w4", operation_id=first.operation.operation_id)
    assert tool.calls == 1 and not second.handler_executed
    assert after.first_dispatch_at == before.first_dispatch_at
    assert after.component_receipts[0].get("provider_message_id") == "provider:known"
    assert second.metadata["handler_details"]["component_receipts"] == after.component_receipts
