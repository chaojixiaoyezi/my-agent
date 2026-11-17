"""S1：请求级运行库连接复用与两级落盘等级（效果前 FULL / 效果后 NORMAL）。"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_py_agent
from agent_py_agent.agent.agent_core.tool_runtime_ledger import persist_tool_runtime_ledger
from agent_py_agent.agent.io.sqlite_scope import (
    SQLiteCommitDurability,
    sqlite_commit_durability,
    sqlite_connection_scope,
)
from agent_py_agent.agent.runtime_db import schema
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="子进程以 SIGKILL 模拟进程死亡")


def _chain(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="owner-a", goal="s1", conversation_task_id="task-a",
                                     thread_id="thread-a", run_id="run-a", role="assistant")
    return repo, str(chain["attempt_id"])


def _ok():
    return ToolHandlerOutcome("synthetic_write", True, json.dumps({"ok": True}))


def _execution_request(store, attempt_id, operation_id, invoke=_ok):
    return ToolOperationExecutionRequest(
        store=store, store_required=True, owner_id="owner-a", run_id="run-a", task_id="task-a",
        operation_id=operation_id, tool_name="synthetic_write", args_hash="args-" + operation_id,
        idempotency_key="key-" + operation_id, idempotency_scope="operation", idempotency_namespace="ns",
        timeout_seconds=30, invoke=invoke, attempt_id=attempt_id)


def _operation_status(db, operation_id):
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute("SELECT status FROM tool_operations WHERE operation_id=?", (operation_id,)).fetchone()[0]


@pytest.fixture
def runtime_connects(monkeypatch):
    """记录运行库每条新物理连接及其语句，供连接寿命与提交等级断言。"""
    real_connect = sqlite3.connect
    log = {"active": False, "connections": []}

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        target = str(args[0] if args else kwargs.get("database", ""))
        if log["active"] and Path(target.split("?")[0].replace("file://", "")).name == "runtime.db":
            statements = []
            log["connections"].append(statements)
            conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return log


# 函数用途: 由逐条语句还原每次 COMMIT 时生效的 synchronous 等级与该事务写入。
def _commits(statements):
    level, writes, commits = "FULL", [], []
    for sql in statements:
        text = " ".join(sql.split())
        upper = text.upper()
        if upper.startswith("PRAGMA SYNCHRONOUS="):
            level = text.split("=", 1)[1].strip().upper()
        elif upper.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(text)
        elif upper.startswith("COMMIT"):
            commits.append((level, tuple(writes)))
            writes = []
    return commits


def test_sqlite_default_synchronous_is_full_for_wal(tmp_path):
    # 未声明的事务沿连接默认等级；本断言证明默认即 FULL，效果前提交不因省略 PRAGMA 而降级。
    with sqlite3.connect(tmp_path / "probe.db") as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_managed_tool_call_uses_one_runtime_connection(tmp_path, runtime_connects):
    repo, attempt_id = _chain(tmp_path)
    store = ManagedOperationStore(repo)
    assert execute_tool_operation(_execution_request(store, attempt_id, "op-warm")).ok
    runtime_connects["active"] = True
    result = execute_tool_operation(_execution_request(store, attempt_id, "op-measured"))
    runtime_connects["active"] = False
    assert result.ok
    # 基线为 5 条（claim、claim 回读、结算载荷、settle、结算回读各一条短连接）。
    assert len(runtime_connects["connections"]) == 1
    commits = _commits(runtime_connects["connections"][0])
    claim = [level for level, writes in commits if any("status = 'EXECUTING'" in w for w in writes)]
    settle = [level for level, writes in commits if any("status = 'SUCCEEDED'" in w for w in writes)]
    assert claim == ["FULL"]
    assert settle == ["NORMAL"]


def test_handler_runs_without_open_transaction_or_pending_wal_frames(tmp_path):
    repo, attempt_id = _chain(tmp_path)
    store = ManagedOperationStore(repo)
    observed = {}

    def invoke():
        wal = Path(f"{repo.db_path}-wal")
        observed["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        # 领取连接仍缓存在作用域内，但归还时已结束事务并截断 WAL：别的写者零等待即可拿到写锁。
        probe = sqlite3.connect(repo.db_path, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            observed["status"] = probe.execute(
                "SELECT status FROM tool_operations WHERE operation_id='op-handler'").fetchone()[0]
        finally:
            probe.close()
        return _ok()

    assert execute_tool_operation(_execution_request(store, attempt_id, "op-handler", invoke)).ok
    assert observed == {"wal_bytes": 0, "status": "EXECUTING"}
    assert _operation_status(repo.db_path, "op-handler") == "SUCCEEDED"


def test_tool_call_joins_the_caller_scope_without_closing_it(tmp_path, runtime_connects):
    repo, attempt_id = _chain(tmp_path)
    store = ManagedOperationStore(repo)
    runtime_connects["active"] = True
    with sqlite_connection_scope(True):
        for operation_id in ("op-a", "op-b"):
            assert execute_tool_operation(_execution_request(store, attempt_id, operation_id)).ok
    runtime_connects["active"] = False
    # 工作项已开作用域时并入它：两次调用仍是调用方的同一条连接，不嵌套新开也不提前关闭。
    assert len(runtime_connects["connections"]) == 1


def test_record_path_reuses_one_connection_and_commits_post_effect(tmp_path, runtime_connects):
    repo, attempt_id = _chain(tmp_path)
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), local_store=None)
    record = {"run_id": "run-a", "operation_id": "op-record", "attempt_id": attempt_id, "tool": "synthetic_write",
              "ok": True, "task_id": "task-a"}
    runtime_connects["active"] = True
    persist_tool_runtime_ledger(agent, record)
    runtime_connects["active"] = False
    # 基线为 3 条（agent_run 查找、事件追加、事件回读各一条短连接）。
    assert len(runtime_connects["connections"]) == 1
    commits = _commits(runtime_connects["connections"][0])
    assert [level for level, writes in commits if any("runtime_events" in w for w in writes)] == ["NORMAL"]
    events = repo.events_for_attempt(attempt_id)
    assert [event["event_type"] for event in events][-1] == "tool_completed"


def test_reused_connection_restores_default_level_for_undeclared_writes(tmp_path, runtime_connects):
    repo, attempt_id = _chain(tmp_path)
    run = repo.agent_run_for_run_id("run-a")
    ids = {"attempt_id": attempt_id, "agent_run_id": str(run["agent_run_id"]), "task_run_id": str(run["task_run_id"])}
    runtime_connects["active"] = True
    with sqlite_connection_scope(True):
        with sqlite_commit_durability(SQLiteCommitDurability.POST_EFFECT):
            repo.append_event(event_type="s1.observed", **ids)
        repo.append_event(event_type="s1.undeclared", **ids)
        with sqlite_commit_durability(SQLiteCommitDurability.PRE_EFFECT):
            repo.append_event(event_type="s1.pre_effect", **ids)
    runtime_connects["active"] = False
    assert len(runtime_connects["connections"]) == 1
    levels = [level for level, writes in _commits(runtime_connects["connections"][0]) if writes]
    assert levels == ["NORMAL", "FULL", "FULL"]


def test_durability_declaration_requires_typed_member():
    with pytest.raises(TypeError):
        with sqlite_commit_durability("NORMAL"):
            pass


def test_fresh_borrow_skips_the_repeat_generation_probe_and_reuse_rechecks(tmp_path, monkeypatch):
    repo, _ = _chain(tmp_path)
    real = schema.require_runtime_schema_generation
    calls = []

    def spy(conn, **kwargs):
        calls.append(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(schema, "require_runtime_schema_generation", spy)
    with sqlite_connection_scope(True):
        with repo._runtime_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        fresh = list(calls)
        with repo._runtime_connection() as conn:
            conn.execute("SELECT 1").fetchone()
    # 新开连接只由 _runtime_connect 核一次代际；复用缓存连接时每次借用仍重核。
    assert fresh == [{"metadata_present": True}]
    assert calls == [{"metadata_present": True}, {}]


def _child_env():
    root = Path(agent_py_agent.__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return env


_CHILD_PRELUDE = textwrap.dedent("""
    import json, os, signal, sys
    from pathlib import Path
    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolOperationExecutionRequest, execute_tool_operation)
    repo = RuntimeRepository(Path(sys.argv[1]))
    chain = repo.record_run_creation(owner_id="owner-a", goal="s1", conversation_task_id="task-a",
                                     thread_id="thread-a", run_id="run-a", role="assistant")
    print(chain["attempt_id"], flush=True)

    def run(invoke):
        return execute_tool_operation(ToolOperationExecutionRequest(
            store=ManagedOperationStore(repo), store_required=True, owner_id="owner-a", run_id="run-a",
            task_id="task-a", operation_id="op-child", tool_name="synthetic_write", args_hash="args-op-child",
            idempotency_key="key-op-child", idempotency_scope="operation", idempotency_namespace="ns",
            timeout_seconds=30, invoke=invoke, attempt_id=str(chain["attempt_id"])))
""")


@_POSIX_ONLY
def test_v4_process_death_between_claim_and_settle_recovers_as_unknown(tmp_path):
    db = tmp_path / "runtime.db"
    script = _CHILD_PRELUDE + "run(lambda: os.kill(os.getpid(), signal.SIGKILL))\n"
    child = subprocess.run([sys.executable, "-c", script, str(db)], env=_child_env(), capture_output=True,
                           text=True, timeout=120)
    assert child.returncode == -signal.SIGKILL, child.stderr
    attempt_id = child.stdout.strip()
    # 领取以 FULL 提交后才进入 handler：进程在 handler 内死亡，EXECUTING 事实仍在库中。
    assert _operation_status(db, "op-child") == "EXECUTING"
    calls = []
    store = ManagedOperationStore(RuntimeRepository(db))
    result = execute_tool_operation(_execution_request(store, attempt_id, "op-child",
                                                       lambda: calls.append("again") or _ok()))
    # 原持有进程已死：同一事务把 EXECUTING 标 UNKNOWN，绝不再次进入 handler。
    assert not result.ok
    assert calls == []
    assert _operation_status(db, "op-child") == "UNKNOWN"


@_POSIX_ONLY
def test_clean_process_exit_leaves_no_orphan_wal(tmp_path):
    db = tmp_path / "runtime.db"
    script = _CHILD_PRELUDE + textwrap.dedent("""
        result = run(lambda: ToolHandlerOutcome("synthetic_write", True, json.dumps({"ok": True})))
        assert result.ok, result
    """)
    child = subprocess.run([sys.executable, "-c", script, str(db)], env=_child_env(), capture_output=True,
                           text=True, timeout=120)
    assert child.returncode == 0, child.stderr
    assert not Path(f"{db}-wal").exists() and not Path(f"{db}-shm").exists()
    assert _operation_status(db, "op-child") == "SUCCEEDED"
