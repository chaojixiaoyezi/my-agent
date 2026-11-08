"""真实 SQLite 验证保留追平、通道余量及物理页回收，不改业务终态。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.optimization_contracts import StoreSchemaNewerError
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
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


def _vacuum_backlog(conn):
    _large_output(conn)
    assert retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=128)["slimmed"] == 1
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] >= maintenance.LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT


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


def test_idle_passes_drain_audited_scale_vacuum_backlog(database, record_testsuite_property):
    home, repo, conn = database
    body = json.dumps({"output": "x" * (128 * 1024)})
    _run(conn)
    for index in range(1024):
        _tool(conn, f"op-{index}", outcome=body)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    initial_size = repo.db_path.stat().st_size
    assert initial_size >= 128 * 1024 * 1024
    cursor, slimmed = {}, 0
    for logical_calls in range(1, 121):
        result = retention.maintain_runtime_history_lane(_request(home, items=512, cursor=cursor))
        cursor = result["cursor"]
        slimmed += result["slimmed"]
        if slimmed == 1024:
            break
    assert slimmed == 1024
    threshold = maintenance.LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT
    free_before = conn.execute("PRAGMA freelist_count").fetchone()[0]
    size_before = repo.db_path.stat().st_size
    assert free_before >= 2 * threshold
    idle_results, free_pages = [], [free_before]
    for _ in range(40):
        result = retention.maintain_runtime_history_lane(_request(home, items=512, cursor=cursor))
        storage_lanes.validate_lane_receipt(result)
        cursor = result["cursor"]
        assert result["slimmed"] == result["deleted_events"] == result["deleted_llm_records"] == 0
        idle_results.append(result)
        free_pages.append(conn.execute("PRAGMA freelist_count").fetchone()[0])
        if free_pages[-1] < threshold and result["complete"]:
            break
    evidence = {"rows": slimmed, "payload_bytes": 1024 * len(body.encode("utf-8")),
                "initial_database_bytes": initial_size, "logical_retention_calls": logical_calls,
                "idle_calls": len(idle_results), "free_pages": free_pages,
                "database_bytes_before_idle": size_before, "database_bytes_after_idle": repo.db_path.stat().st_size}
    record_testsuite_property("runtime_vacuum_idle_continuation", json.dumps(evidence, sort_keys=True))
    assert free_pages[-1] < threshold, evidence
    assert idle_results[-1]["complete"], evidence
    assert repo.db_path.stat().st_size < size_before, evidence
    assert sum(result.get("freed_bytes", 0) for result in idle_results) > 0
    assert sum(after < before for before, after in zip(free_pages, free_pages[1:])) > 1
    assert all(not result["complete"] for result, free in zip(idle_results, free_pages[1:]) if free >= threshold)
    assert conn.execute("SELECT count(*) FROM tool_operations WHERE status='SUCCEEDED' AND updated_at=201"
                        ).fetchone()[0] == 1024
    assert conn.execute("SELECT status,updated_at FROM agent_runs").fetchone() == ("done", 101)


@pytest.mark.parametrize("reason", ["busy", "budget_exhausted"])
def test_last_mutation_deferral_resumes_without_further_logical_changes(database, monkeypatch, reason):
    home, repo, conn = database
    _vacuum_backlog(conn)
    _llm(conn, "last-mutation", owner=home.owner_id)
    conn.commit()
    before = repo.db_path.stat().st_size
    clock = SimpleNamespace(monotonic=time.monotonic)
    monkeypatch.setattr(storage_lanes, "time", clock)
    calls = []

    def commit(operation):
        calls.append(True)
        if len(calls) == 4:
            assert reason == "busy"
            # 最后一个逻辑批次已提交，再由真实 SQLite 写锁阻挡页回收。
            conn.execute("BEGIN IMMEDIATE")
            try:
                return operation()
            finally:
                conn.rollback()
        result = operation()
        if len(calls) == 3 and reason == "budget_exhausted":
            clock.monotonic = lambda: time.monotonic() + 60
        return result

    deferred = retention.maintain_runtime_history_lane(_request(home, commit=commit))
    storage_lanes.validate_lane_receipt(deferred)
    assert deferred["deleted_llm_records"] == 1
    assert deferred["slimmed"] == deferred["deleted_events"] == 0
    assert len(calls) == (4 if reason == "busy" else 3)
    assert not deferred["complete"] and deferred["deferred_reason"] == reason
    assert conn.execute("SELECT count(*) FROM llm_call_records").fetchone()[0] == 0
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] >= maintenance.LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT
    clock.monotonic = time.monotonic
    resumed_calls = []

    def resume(operation):
        resumed_calls.append(True)
        return operation()

    result = retention.maintain_runtime_history_lane(_request(home, cursor=deferred["cursor"], commit=resume))
    assert result["slimmed"] == result["deleted_events"] == result["deleted_llm_records"] == 0
    assert result["complete"] and result["freed_bytes"] > 0
    assert len(resumed_calls) == 4
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] < maintenance.LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT
    assert repo.db_path.stat().st_size < before


def test_idle_small_database_adds_no_physical_write(database):
    home, repo, conn = database
    retention.maintain_runtime_history_lane(_request(home))
    before = repo.db_path.stat().st_size
    for _ in range(3):
        commits = []

        def commit(operation):
            commits.append(True)
            return operation()

        result = retention.maintain_runtime_history_lane(_request(home, commit=commit))
        assert result["complete"] and result["items"] == 0
        assert result.get("freed_bytes", 0) == 0
        assert "deferred_reason" not in result
        assert len(commits) == 3  # 仅有既存三个类别的逻辑批次，没有额外页维护提交。
        assert repo.db_path.stat().st_size == before
        assert not conn.in_transaction


@pytest.mark.parametrize("reason", ["request_hold", "policy_hold", "maintenance_disabled", "categories_disabled"])
def test_protected_vacuum_backlog_never_enters_write_gate(database, reason):
    home, repo, conn = database
    _vacuum_backlog(conn)
    policy = MemoryRetentionPolicy()
    if reason == "policy_hold":
        policy = replace(policy, legal_hold=True)
    elif reason == "maintenance_disabled":
        policy = replace(policy, maintenance_enabled=False)
    elif reason == "categories_disabled":
        policy = replace(policy, tool_output_days_after_terminal=0, audit_days=0)
    home.owner_retention_json.write_text(json.dumps(policy.to_dict()), encoding="utf-8")
    before = (repo.db_path.stat().st_size, conn.execute("PRAGMA freelist_count").fetchone()[0])

    def unexpected_write(operation):
        pytest.fail("保全或关闭维护的库仍进入了写门")

    result = retention.maintain_runtime_history_lane(_request(
        home, legal_hold=reason == "request_hold", commit=unexpected_write))
    assert result["complete"] and result["items"] == 0
    assert (repo.db_path.stat().st_size, conn.execute("PRAGMA freelist_count").fetchone()[0]) == before


def test_checkpoint_continues_after_reader_defers_last_eligible_pages(database):
    home, repo, conn = database
    _large_output(conn)
    before = repo.db_path.stat().st_size
    with closing(sqlite3.connect(repo.db_path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM tool_operations").fetchone()
        retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=128)
        result = retention.maintain_runtime_history_lane(_request(home))
        assert result["slimmed"] == result["deleted_events"] == result["deleted_llm_records"] == 0
        assert not result["complete"] and result["deferred_reason"] == "vacuum_pending"
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] < maintenance.LOCAL_STORE_INCREMENTAL_VACUUM_PAGES_COUNT
        assert repo.db_path.stat().st_size == before
    resumed = retention.maintain_runtime_history_lane(_request(home, cursor=result["cursor"]))
    assert resumed["slimmed"] == resumed["deleted_events"] == resumed["deleted_llm_records"] == 0
    assert resumed["complete"] and resumed["freed_bytes"] > 0
    with repo._runtime_connection() as observer:
        state = maintenance._local_store_vacuum_state(observer)
    assert not maintenance._local_store_checkpoint_pending(repo.db_path, state)
    assert repo.db_path.stat().st_size < before


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


@pytest.mark.parametrize("manual_cursor", [False, True], ids=["connection", "cursor"])
@pytest.mark.parametrize("commit", [False, True], ids=["rollback", "commit"])
@pytest.mark.parametrize("scoped", [False, True], ids=["short", "leased"])
def test_incremental_vacuum_finishes_page_budget_without_implicit_commit(database, manual_cursor, commit, scoped):
    _, repo, observer = database
    _vacuum_backlog(observer)
    observer.execute("INSERT INTO metadata(key,value) VALUES('vacuum_witness','0')")
    observer.commit()
    before = observer.execute("PRAGMA freelist_count").fetchone()[0]
    with sqlite_connection_scope(enabled=scoped), repo._runtime_connection() as conn:
        conn.execute("UPDATE metadata SET value='1' WHERE key='vacuum_witness'")
        assert conn.in_transaction
        with closing(conn.cursor()) as cursor:
            executor = cursor if manual_cursor else conn
            executor.execute("PRAGMA incremental_vacuum(16)").fetchall()
        assert conn.in_transaction
        assert before - conn.execute("PRAGMA freelist_count").fetchone()[0] == 16
        if commit:
            conn.commit()
        else:
            conn.rollback()
        assert not conn.in_transaction
    assert before - observer.execute("PRAGMA freelist_count").fetchone()[0] == (16 if commit else 0)
    assert observer.execute("SELECT value FROM metadata WHERE key='vacuum_witness'").fetchone()[0] == (
        "1" if commit else "0")


@pytest.mark.parametrize("statement", ["VACUUM", "PRAGMA wal_checkpoint(PASSIVE)", "PRAGMA wal_checkpoint(TRUNCATE)",
                                      "PRAGMA incremental_vacuum(16)"])
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
