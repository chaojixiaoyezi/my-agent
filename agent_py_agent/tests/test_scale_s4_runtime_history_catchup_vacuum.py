"""真实 SQLite 验证保留追平、通道余量及物理页回收，不改业务终态。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_py_agent.agent.contracts.optimization_contracts import StoreSchemaNewerError
from agent_py_agent.agent.local_storage import maintenance
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES
from agent_py_agent.agent.runtime_db import history_retention as retention
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest


@pytest.fixture
def database(tmp_path):
    home = home_paths(tmp_path / "isolated")
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()), encoding="utf-8")
    repo = RuntimeRepository(home.owner_home_dir / "runtime.db")
    conn = sqlite3.connect(repo.db_path)
    try:
        yield home, repo, conn
    finally:
        conn.close()


@pytest.fixture
def legacy_database(tmp_path):
    home = home_paths(tmp_path / "legacy")
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()), encoding="utf-8")
    path = home.owner_home_dir / "runtime.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE legacy_marker(value TEXT)")
    conn.commit()
    repo = RuntimeRepository(path)
    try:
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        yield home, repo, conn
    finally:
        conn.close()


def _run(conn, identity="run", *, status="done", task="task", attempt="done"):
    conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (task, "task-" + task, status, 100, 101))
    conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (identity, task, status, 100, 101))
    conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) "
                 "VALUES(?,?,?,?,?,?)", ("attempt-" + identity, identity, 1, attempt, 100, 101))


def _tool(conn, identity="op", *, run="run", status="SUCCEEDED", settled=200, created=100, updated=201,
          outcome=None):
    outcome = json.dumps({"result": "旧结果" * 200}, ensure_ascii=False) if outcome is None else outcome
    conn.execute("INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,"
                 "tool_operation_generation,operation_type,status,settled_at,outcome_json,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, run, "attempt-" + run, 1, 1, "test_tool", status, settled, outcome, created, updated))
    return outcome


def _event(conn, identity, *, run="run", task="task", created=100):
    conn.execute("INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,task_run_id,created_at) "
                 "VALUES(?,?,?,?,?,?)", (identity, "test.pending", "attempt-" + run if run else "", run, task, created))


def _llm(conn, identity, *, owner="owner", created=100, status="completed"):
    conn.execute("INSERT INTO llm_call_records(record_id,call_id,owner_id,status,started_at,finished_at,created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (identity, identity, owner, status, created - 1, created, created))


def _request(home, *, now=200 * 86400, items=None, deadline=None, cursor=None, legal_hold=False, commit=None):
    return StorageLaneRequest("runtime_history", home, now, cursor or {}, StorageLaneBudget(items, None, deadline),
                              legal_hold, commit is not None, None, commit)


def _fill(home, conn, count):
    _run(conn)
    for index in range(count):
        _tool(conn, f"op-{index:04d}")
        _event(conn, f"event-{index:04d}")
        _llm(conn, f"llm-{index:04d}", owner=home.owner_id)
    conn.commit()


def _large_output(conn):
    _run(conn)
    _tool(conn, outcome=json.dumps({"output": "x" * (13 * 1024 * 1024)}))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def test_catchup_uses_multiple_short_batches_and_reaches_complete(database, monkeypatch):
    home, _, conn = database
    _fill(home, conn, 600)
    monkeypatch.setattr(storage_lanes, "STORAGE_LANE_SLICE_MS", 60000)
    results = []
    for _ in range(8):
        result = retention.maintain_runtime_history_lane(_request(home, items=2000))
        results.append(result)
        assert result["items"] <= 2000 - storage_lanes.STORAGE_LANE_SLICE_ENTRIES
        assert sum(result[key] for key in ("slimmed", "deleted_events", "deleted_llm_records")) <= result["items"]
        if result["complete"]:
            break
    assert results[0]["items"] > RETENTION_SCAN_SLICE_ENTRIES
    assert results[-1]["complete"]
    assert sum(result["slimmed"] for result in results) == 600
    assert sum(result["deleted_events"] for result in results) == 600
    assert sum(result["deleted_llm_records"] for result in results) == 600


def test_round_one_preserves_the_six_item_split(database):
    home, _, conn = database
    _fill(home, conn, 10)
    result = retention.maintain_runtime_history_lane(_request(home, items=6))
    assert result["items"] == 6 and not result["complete"]
    assert all(result[key] == 2 for key in ("slimmed", "deleted_events", "deleted_llm_records"))


def test_runtime_history_reserves_the_following_lane_share(database, monkeypatch):
    home, _, conn = database
    _fill(home, conn, 100)
    monkeypatch.setattr(storage_lanes, "STORAGE_LANE_SLICE_MS", 60000)
    result = retention.maintain_runtime_history_lane(_request(home, items=200))
    assert result["items"] <= RETENTION_SCAN_SLICE_ENTRIES
    assert not result["complete"]


def test_fresh_runtime_database_enables_incremental_vacuum(database):
    _, _, conn = database
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_incremental_vacuum_reclaims_physical_database_pages(database, monkeypatch):
    home, repo, conn = database
    _large_output(conn)
    before = repo.db_path.stat().st_size
    assert before >= 12 * 1024 * 1024
    monkeypatch.setattr(storage_lanes, "STORAGE_LANE_SLICE_MS", 60000)
    results = []
    for _ in range(8):
        result = retention.maintain_runtime_history_lane(_request(home, items=2000))
        results.append(result)
        if result["complete"]:
            break
    assert results[-1]["complete"]
    assert any(result.get("freed_bytes", 0) > 0 for result in results)
    assert repo.db_path.stat().st_size < before
    assert json.loads(conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0])["retention"] == "slimmed"


def test_legacy_coordinator_never_converts_or_adds_a_vacuum_commit(legacy_database, monkeypatch):
    home, repo, conn = legacy_database
    _large_output(conn)
    _event(conn, "event")
    _llm(conn, "llm", owner=home.owner_id)
    conn.commit()
    monkeypatch.setattr(storage_lanes, "STORAGE_LANE_SLICE_MS", 60000)
    commits = []

    def commit(operation):
        commits.append(True)
        return operation()

    result = retention.maintain_runtime_history_lane(_request(home, items=6, commit=commit))
    assert result["complete"] and result["items"] == 3
    assert all(result[key] == 1 for key in ("slimmed", "deleted_events", "deleted_llm_records"))
    assert len(commits) == 3
    with repo._runtime_connection() as observer:
        assert observer.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert result.get("freed_bytes", 0) == 0


def test_legacy_explicit_maintenance_can_convert_a_small_database(legacy_database, monkeypatch):
    home, repo, conn = legacy_database
    _large_output(conn)
    monkeypatch.setattr(storage_lanes, "STORAGE_LANE_SLICE_MS", 60000)
    result = retention.maintain_runtime_history_lane(_request(home, items=2000))
    assert result["slimmed"] == 1
    # SQLite 的旧连接缓存 auto_vacuum；用新连接核实维护连接已提交的物理库模式。
    with repo._runtime_connection() as observer:
        assert observer.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='legacy_marker'").fetchone()


def test_page_state_reads_do_not_open_a_write_transaction(database):
    _, repo, _ = database
    with repo._runtime_connection() as conn:
        assert not conn.in_transaction
        assert maintenance._local_store_vacuum_state(conn)[0] == 2
        assert not conn.in_transaction
        conn.execute("PRAGMA user_version=0")
        assert conn.in_transaction
        conn.rollback()


@pytest.mark.parametrize("statement", ["VACUUM", "PRAGMA wal_checkpoint(PASSIVE)", "PRAGMA wal_checkpoint(TRUNCATE)"])
def test_transaction_free_page_maintenance_still_checks_schema_generation(database, statement):
    _, repo, writer = database
    with repo._runtime_connection() as conn:
        writer.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_generation','2')")
        writer.commit()
        with pytest.raises(StoreSchemaNewerError, match="STORE_SCHEMA_NEWER"):
            conn.execute(statement).fetchall()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_ERROR])
def test_vacuum_defers_only_busy_or_locked_errors(database, monkeypatch, code):
    home, repo, _ = database
    error = sqlite3.OperationalError("maintenance test")
    error.sqlite_errorcode = code

    def fail(_conn):
        raise error

    monkeypatch.setattr(maintenance, "_local_store_vacuum_state", fail)
    request = _request(home)
    if code == sqlite3.SQLITE_ERROR:
        with pytest.raises(sqlite3.OperationalError, match="maintenance test"):
            retention._runtime_history_vacuum(repo, Path(repo.db_path), request, request.budget)
    else:
        assert retention._runtime_history_vacuum(repo, Path(repo.db_path), request, request.budget) == 0
