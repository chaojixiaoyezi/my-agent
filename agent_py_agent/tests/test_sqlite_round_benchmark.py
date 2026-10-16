"""E2：相同回合操作可在真实基线与当前工作树上测量；计时不作通过门槛。"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import nullcontext

import pytest

from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.support.ci_release_layout import assert_business_module

# 基线没有作用域模块；这里只让同一个观察脚本测量两版，不改产品回退路径。
try:
    from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
except ModuleNotFoundError:
    sqlite_connection_scope = None


def _runtime_operations(path, index):
    repo = RuntimeRepository(path)
    task = repo.create_task(owner_id="sample-owner", task_id=f"task-{index}")
    run = repo.create_task_run(task_id=task["task_id"], status="running")
    for _ in range(5):
        assert repo.get_task_run(run["task_run_id"])["status"] == "running"


def _local_operations(path, index):
    store = LocalStore(path)
    store.upsert_record(source_type="memory", source_id=f"sample-{index}",
                        title="连贯记录", content=f"回合事实 {index}")
    for _ in range(5):
        with store._connection() as conn:
            assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == index + 1


def _common_round(paths):
    for index in range(10):
        _runtime_operations(paths[0], index)
        _local_operations(paths[1], index)


def _measure(tmp_path, monkeypatch, enabled):
    paths = tmp_path / "runtime.db", tmp_path / "local.db"
    RuntimeRepository(paths[0])
    LocalStore(paths[1])
    statements, events = [], []
    original = sqlite3.connect
    active = [True]

    def audit(event, args):
        if active[0] and event == "sqlite3.connect":
            events.append(str(args[0]))

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    sys.addaudithook(audit)
    monkeypatch.setattr(sqlite3, "connect", traced)
    scope = sqlite_connection_scope(enabled) if sqlite_connection_scope else nullcontext()
    started = time.perf_counter()
    try:
        with scope:
            _common_round(paths)
    finally:
        active[0] = False
    elapsed = time.perf_counter() - started
    counts = {
        "ddl": sum(sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP")) for sql in statements),
        "connect": len(events), "elapsed_ms": round(elapsed * 1000, 3),
        "scope_available": sqlite_connection_scope is not None, "reuse": enabled,
    }
    return paths, counts


@pytest.mark.parametrize("enabled", [False, True], ids=["reuse-off", "reuse-on"])
def test_common_round_preserves_facts_and_reports_actual_counts(tmp_path, monkeypatch, enabled):
    # 源码必须属于当前 checkout；发行只认同解释器隔离 runtime 的真实 .pyc，不使用 code.co_filename 旧源码路径。
    import agent_py_agent.agent.runtime_db.repository as runtime_repository

    assert_business_module(runtime_repository)
    paths, counts = _measure(tmp_path, monkeypatch, enabled)
    with sqlite3.connect(paths[0]) as conn:
        assert conn.execute("SELECT count(*) FROM task_runs WHERE status='running'").fetchone()[0] == 10
    with sqlite3.connect(paths[1]) as conn:
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 10
    print("E2_ROUND_METRICS=" + json.dumps(counts, sort_keys=True))
