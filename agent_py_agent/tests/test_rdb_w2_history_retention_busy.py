"""真实 SQLite 写锁和确定性预算验证通道重试、延期、回滚及补跑闭包。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.runtime_db import history_retention as retention
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_cat3_runtime_history_retention import (
    _event,
    _request,
    _run,
    _tool,
)


@pytest.fixture
def database(tmp_path):
    home = home_paths(tmp_path / "isolated")
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()), encoding="utf-8")
    repo = RuntimeRepository(home.owner_home_dir / "runtime.db")
    with closing(sqlite3.connect(repo.db_path)) as conn:
        yield home, repo, conn


def _seed(conn, *, events=False):
    _run(conn)
    for index in range(3):
        _tool(conn, f"tool-{index}")
        if events:
            _event(conn, f"event-{index}")
    conn.commit()


def _cursors(conn):
    return conn.execute("SELECT key,value FROM metadata WHERE key LIKE 'history_retention_cursor:%' ORDER BY key").fetchall()


def _error(code, message="opaque SQLite failure"):
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


def _virtual_clock(monkeypatch, *, on_sleep=None):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        assert 0 < seconds < float("inf")
        if on_sleep is not None:
            on_sleep()
        clock.sleeps.append(seconds)
        clock.now += seconds

    provider = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    monkeypatch.setattr(retention, "time", provider, raising=False)
    monkeypatch.setattr(storage_lanes, "time", provider)
    return clock


def test_lane_retries_short_busy_and_commits(database, monkeypatch):
    home, repo, conn = database
    _seed(conn)
    attempts, busy, failures = [], [], []
    gate = SimpleNamespace(held=False)
    released = threading.Event()
    with closing(sqlite3.connect(repo.db_path, timeout=0, check_same_thread=False)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")

        def release():
            try:
                blocker.rollback()
            except BaseException as error:
                failures.append(error)
            finally:
                released.set()

        timer = threading.Timer(0.02, release)

        def commit(operation):
            gate.held = True
            attempts.append(operation)
            try:
                return operation()
            except sqlite3.OperationalError as error:
                assert sqlite_error_details(error)["category"] == "busy"
                busy.append(error)
                if len(busy) == 1:
                    timer.start()
                raise
            finally:
                gate.held = False

        sleeps = []

        def sleep(seconds):
            assert not gate.held, "重试等待不得占用原通道写门"
            sleeps.append(seconds)
            time.sleep(seconds)

        monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep), raising=False)
        try:
            result = retention.maintain_runtime_history_lane(_request(
                home, items=3, deadline=time.monotonic() + 1, commit=commit,
            ))
        finally:
            timer.cancel()
            if timer.ident is not None:
                timer.join(timeout=5)
        assert released.is_set() and not timer.is_alive() and not failures
    storage_lanes.validate_lane_receipt(result)
    assert busy and sleeps and result["slimmed"] >= 1
    assert len(attempts) >= 2
    assert int(dict(_cursors(conn))["history_retention_cursor:tool_operations"]) >= 1
    assert json.loads(conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id='tool-0'").fetchone()[0])["retention"] == "slimmed"


@pytest.mark.parametrize("existing_cursor", [False, True])
def test_lane_defers_on_long_busy_without_raising(database, existing_cursor):
    home, repo, conn = database
    _seed(conn)
    if existing_cursor:
        retention._save_history_cursor(conn, "tool_operations", 1)
        retention._save_history_cursor(conn, "runtime_events", 17)
        conn.commit()
    before = _cursors(conn)
    outcomes = conn.execute("SELECT operation_id,outcome_json FROM tool_operations ORDER BY operation_id").fetchall()
    with closing(sqlite3.connect(repo.db_path, timeout=0)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        result = retention.maintain_runtime_history_lane(_request(
            home, items=3, deadline=time.monotonic() + 1,
        ))
        storage_lanes.validate_lane_receipt(result)
        assert result["complete"] is False and result["deferred_reason"] == "busy"
        assert result["slimmed"] == result["items"] == result["deleted_events"] == 0
        assert _cursors(conn) == before
        assert conn.execute("SELECT operation_id,outcome_json FROM tool_operations ORDER BY operation_id").fetchall() == outcomes
        blocker.rollback()
    resumed = retention.maintain_runtime_history_lane(_request(home, items=3, deadline=time.monotonic() + 1))
    assert resumed["slimmed"] >= 1


@pytest.mark.parametrize("kind", ["locked", "structural", "other", "message_only", "cleanup_structural"])
def test_lane_does_not_retry_locked_or_other_errors(database, monkeypatch, kind):
    home, _, conn = database
    _seed(conn)
    error = {
        "locked": _error(sqlite3.SQLITE_LOCKED),
        "structural": sqlite3.DatabaseError("structural SQLite failure"),
        "other": _error(sqlite3.SQLITE_ERROR),
        "message_only": sqlite3.OperationalError("database is locked"),
        "cleanup_structural": _error(sqlite3.SQLITE_BUSY),
    }[kind]
    if kind == "cleanup_structural":
        error.sqlite_cleanup_error = sqlite3.DatabaseError("structural cleanup")
        error.sqlite_cleanup_error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    calls = []

    def commit(operation):
        calls.append(operation)
        raise error

    def no_sleep(_seconds):
        pytest.fail("locked、结构性或未知错误不应退避重试")

    monkeypatch.setattr(retention, "time", SimpleNamespace(monotonic=time.monotonic, sleep=no_sleep), raising=False)
    request = _request(home, deadline=time.monotonic() + 1, commit=commit)
    before = _cursors(conn)
    if kind == "locked":
        result = retention.maintain_runtime_history_lane(request)
        storage_lanes.validate_lane_receipt(result)
        assert result["complete"] is False and result["deferred_reason"] == "busy"
        assert result["slimmed"] == 0
    else:
        with pytest.raises(type(error)) as caught:
            retention.maintain_runtime_history_lane(request)
        assert caught.value is error
    assert len(calls) == 1 and _cursors(conn) == before


def test_catchup_busy_retains_all_pending_tables_and_their_committed_cursors(database, monkeypatch):
    home, _, conn = database
    _seed(conn, events=True)
    clock = _virtual_clock(monkeypatch)
    calls = []

    def commit(operation):
        calls.append(operation)
        if len(calls) == 4:
            raise _error(sqlite3.SQLITE_LOCKED)
        return operation()

    result = retention.maintain_runtime_history_lane(_request(home, items=6, deadline=1, commit=commit))
    storage_lanes.validate_lane_receipt(result)
    assert len(calls) == 4 and not clock.sleeps
    assert result["complete"] is False and result["deferred_reason"] == "busy"
    assert result["slimmed"] == result["deleted_events"] == 2
    assert result["cursor"]["tool_operations"] == result["cursor"]["runtime_events"] == 2
    persisted = dict(_cursors(conn))
    assert persisted["history_retention_cursor:tool_operations"] == "2"
    assert persisted["history_retention_cursor:runtime_events"] == "2"
    resumed = retention.maintain_runtime_history_lane(_request(home, items=6, deadline=1))
    assert resumed["slimmed"] == resumed["deleted_events"] == 1
    assert conn.execute("SELECT count(*) FROM runtime_events").fetchone()[0] == 0


def test_catchup_retry_uses_capped_budget_and_keeps_step_closure_values(database, monkeypatch):
    home, _, conn = database
    _seed(conn, events=True)
    gate = SimpleNamespace(held=False)
    clock = _virtual_clock(monkeypatch, on_sleep=lambda: pytest.fail("写门内等待") if gate.held else None)
    calls, steps = [], []
    original_tool = retention.slim_terminal_tool_outcomes_step
    original_events = retention.prune_terminal_runtime_events_step

    def tools(conn, **kwargs):
        steps.append(("tool_operations", kwargs["max_rows"], kwargs["cutoff"]))
        return original_tool(conn, **kwargs)

    def events(conn, **kwargs):
        steps.append(("runtime_events", kwargs["max_rows"], kwargs["cutoff"]))
        return original_events(conn, **kwargs)

    monkeypatch.setattr(retention, "slim_terminal_tool_outcomes_step", tools)
    monkeypatch.setattr(retention, "prune_terminal_runtime_events_step", events)

    def commit(operation):
        calls.append(operation)
        gate.held = True
        try:
            if len(calls) == 4:
                raise _error(sqlite3.SQLITE_BUSY)
            return operation()
        finally:
            gate.held = False

    result = retention.maintain_runtime_history_lane(_request(home, items=6, deadline=1, commit=commit))
    assert result["slimmed"] == result["deleted_events"] == 3
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] <= storage_lanes.STORAGE_LANE_SLICE_MS / 1000
    assert steps[:4] == [
        ("tool_operations", 2, (200 - 30) * 86400),
        ("runtime_events", 2, (200 - 180) * 86400),
        ("tool_operations", 1, (200 - 30) * 86400),
        ("runtime_events", 1, (200 - 180) * 86400),
    ]


def test_busy_after_mutation_rolls_back_before_retry(database, monkeypatch):
    home, _, conn = database
    _seed(conn)
    before = _cursors(conn)
    original_outcome = conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id='tool-0'").fetchone()[0]
    original_save = retention._save_history_cursor
    failures = []

    def save(connection, table, cursor):
        original_save(connection, table, cursor)
        if table == "tool_operations" and not failures:
            failures.append(True)
            raise _error(sqlite3.SQLITE_BUSY)

    def rolled_back():
        assert _cursors(conn) == before
        assert conn.execute("SELECT outcome_json FROM tool_operations WHERE operation_id='tool-0'").fetchone()[0] == original_outcome

    monkeypatch.setattr(retention, "_save_history_cursor", save)
    clock = _virtual_clock(monkeypatch, on_sleep=rolled_back)
    result = retention.maintain_runtime_history_lane(_request(home, items=3, deadline=1))
    assert failures and clock.sleeps and result["slimmed"] >= 1
    assert _cursors(conn) != before


def test_no_deadline_uses_existing_lane_cap_when_busy(database, monkeypatch):
    home, _, conn = database
    _seed(conn)
    clock = _virtual_clock(monkeypatch)
    calls = []

    def commit(operation):
        calls.append(operation)
        raise _error(sqlite3.SQLITE_BUSY)

    request = _request(home, commit=commit)
    result = retention.maintain_runtime_history_lane(request)
    storage_lanes.validate_lane_receipt(result)
    assert result["complete"] is False and result["deferred_reason"] == "busy"
    assert 1 < len(calls) < 100
    assert 0 < clock.now <= storage_lanes.STORAGE_LANE_SLICE_MS / 1000
    assert request.budget.deadline is None and _cursors(conn) == []
