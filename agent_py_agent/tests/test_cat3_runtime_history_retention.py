"""真实 SQLite 小批保留、恢复游标、预算与事务回滚的运行时回归。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.runtime_db import history_retention as retention
from agent_py_agent.agent.runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
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


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_tool_slimming_preserves_state_timestamp_and_exact_original_digest(database, status):
    _, _, conn = database
    _run(conn)
    original = _tool(conn, status=status)
    conn.commit()
    receipt = retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=8)
    stored, actual_status, updated = conn.execute(
        "SELECT outcome_json,status,updated_at FROM tool_operations").fetchone()
    raw = original.encode("utf-8")
    assert json.loads(stored) == {"retention": "slimmed", "original_sha256": hashlib.sha256(raw).hexdigest(),
                                 "original_bytes": len(raw)}
    assert (actual_status, updated) == (status, 201)
    assert receipt == {"cursor": 0, "complete": True, "items": 1,
                       "bytes": len(raw) - len(stored.encode("utf-8")), "slimmed": 1}
    second = retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=8)
    assert second["slimmed"] == second["bytes"] == 0
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == stored


@pytest.mark.parametrize("run_status", sorted(AGENT_RUN_TERMINAL_STATUSES))
def test_each_canonical_terminal_run_can_be_slimmed(database, run_status):
    _, _, conn = database
    _run(conn, status=run_status)
    _tool(conn)
    conn.commit()
    assert retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=8)["slimmed"] == 1


@pytest.mark.parametrize("run_status,options", [
    ("running", {}), ("unknown", {}), ("", {}), ("DONE", {}),
    ("done", {"status": "UNKNOWN"}), ("done", {"status": "EXECUTING"}),
    ("done", {"status": "CLAIMED"}), ("done", {"status": "succeeded"}),
    ("done", {"settled": 0}), ("done", {"settled": 1000}),
    ("done", {"created": 1000}), ("done", {"run": "missing"}),
])
def test_tool_unsettled_unknown_recent_or_unproven_rows_are_untouched(database, run_status, options):
    _, _, conn = database
    _run(conn, status=run_status)
    original = _tool(conn, **options)
    conn.commit()
    receipt = retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=8)
    assert receipt["slimmed"] == receipt["bytes"] == 0
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original


@pytest.mark.parametrize("table,step", [
    ("tool_operations", retention.slim_terminal_tool_outcomes_step),
    ("runtime_events", retention.prune_terminal_runtime_events_step),
])
def test_persisted_cursor_resumes_across_connections_and_resets_at_tail(database, table, step):
    _, repo, conn = database
    _run(conn)
    for index in range(5):
        (_tool if table == "tool_operations" else _event)(conn, str(index))
    conn.commit()
    first = step(conn, cutoff=1000, max_rows=2)
    assert (first["cursor"], first["complete"], first["items"]) == (2, False, 2)
    with repo.transaction() as second_connection:
        second = step(second_connection, cutoff=1000, max_rows=2)
    assert (second["cursor"], second["complete"], second["items"]) == (4, False, 2)
    with repo.transaction() as third_connection:
        third = step(third_connection, cutoff=1000, max_rows=2)
    assert (third["cursor"], third["complete"], third["items"]) == (0, True, 1)
    assert conn.execute("SELECT value FROM metadata WHERE key=?",
                        ("history_retention_cursor:" + table,)).fetchone()[0] == "0"


@pytest.mark.parametrize("table,step", [
    ("tool_operations", retention.slim_terminal_tool_outcomes_step),
    ("runtime_events", retention.prune_terminal_runtime_events_step),
])
def test_recent_row_stops_before_it_even_when_later_rows_are_old(database, table, step):
    _, _, conn = database
    _run(conn)
    add = _tool if table == "tool_operations" else _event
    add(conn, "old")
    add(conn, "boundary", created=1000)
    add(conn, "later-old")
    conn.commit()
    result = step(conn, cutoff=1000, max_rows=8)
    assert (result["cursor"], result["complete"], result["items"]) == (1, True, 2)
    again = step(conn, cutoff=1000, max_rows=8)
    assert (again["cursor"], again["items"]) == (1, 1)
    column = "outcome_json" if table == "tool_operations" else "event_id"
    assert conn.execute(f"SELECT {column} FROM {table} WHERE rowid=3").fetchone() is not None


def test_event_pruning_protects_active_attempts_unknown_ids_and_pending_recovery(database):
    _, repo, conn = database
    _run(conn)
    _run(conn, "active", status="running", task="active-task", attempt="running")
    _run(conn, "unsettled", task="unsettled-task", attempt="running")
    _event(conn, "terminal")
    _event(conn, "active", run="active", task="active-task")
    _event(conn, "unsettled", run="unsettled", task="unsettled-task")
    _event(conn, "task-only", run="", task="task")
    _event(conn, "active-task-only", run="", task="active-task")
    _event(conn, "anonymous", run="", task="")
    _event(conn, "missing-run", run="missing", task="task")
    _event(conn, "missing-task", run="", task="missing")
    _event(conn, "recent", created=1000)
    conn.commit()
    before = repo.pending_events_page("test.pending", consumed_event_type="test.consumed", consumer="retention-test")
    result = retention.prune_terminal_runtime_events_step(conn, cutoff=1000, max_rows=30)
    assert result["deleted_events"] == 2
    remaining = {row[0] for row in conn.execute("SELECT event_id FROM runtime_events")}
    assert remaining == {"active", "unsettled", "active-task-only", "anonymous", "missing-run", "missing-task", "recent"}
    after = repo.pending_events_page("test.pending", consumed_event_type="test.consumed", consumer="retention-test")
    assert "active" in {row["event_id"] for row in before} & {row["event_id"] for row in after}
    assert conn.execute("SELECT value FROM metadata WHERE key LIKE 'event_recovery_cursor:%'").fetchone() is not None


def test_event_boundaries_keep_autoincrement_watermark_after_all_rows_are_deleted(database):
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService

    _, _, conn = database
    bounds = ("SELECT min(seq),COALESCE(max(seq),"
              "(SELECT seq FROM sqlite_sequence WHERE name='runtime_events'),0) FROM runtime_events")
    assert conn.execute(bounds).fetchone() == (None, 0)
    assert BusinessObservationService._event_bounds(None, conn) == (None, 0)
    _run(conn)
    _event(conn, "one")
    _event(conn, "two")
    conn.commit()
    assert retention.prune_terminal_runtime_events_step(conn, cutoff=1000, max_rows=1)["deleted_events"] == 1
    assert conn.execute(bounds).fetchone() == (2, 2)
    retention.prune_terminal_runtime_events_step(conn, cutoff=1000, max_rows=8)
    assert conn.execute(bounds).fetchone() == (None, 2)
    assert BusinessObservationService._event_bounds(None, conn) == (None, 2)
    _event(conn, "new", created=2000)
    conn.commit()
    assert conn.execute(bounds).fetchone() == (3, 3)
    assert BusinessObservationService._event_bounds(None, conn) == (3, 3)


def test_llm_records_use_owner_cutoff_terminal_states_and_existing_time_index(database):
    _, _, conn = database
    for status in ("completed", "failed", "cancelled", "running", "finished", "unknown", "done"):
        _llm(conn, status, status=status)
    _llm(conn, "other-owner", owner="other")
    _llm(conn, "boundary", created=1000)
    conn.commit()
    plan = conn.execute("EXPLAIN QUERY PLAN SELECT record_id FROM llm_call_records "
                        "WHERE owner_id=? AND created_at<? AND status IN ('completed','failed','cancelled') "
                        "ORDER BY created_at,record_id LIMIT ?", ("owner", 1000, 2)).fetchall()
    assert any("idx_llm_records_owner_created" in row[3] for row in plan)
    first = retention.prune_llm_call_records_step(conn, owner_id="owner", cutoff=1000, max_rows=2)
    assert first["deleted_llm_records"] == first["items"] == 2 and not first["complete"]
    second = retention.prune_llm_call_records_step(conn, owner_id="owner", cutoff=1000, max_rows=2)
    assert second["deleted_llm_records"] == 1 and second["complete"]
    assert {row[0] for row in conn.execute("SELECT record_id FROM llm_call_records")} == {
        "other-owner", "boundary", "running", "finished", "unknown", "done"}


def test_llm_absent_table_is_not_recreated(database):
    _, _, conn = database
    conn.execute("DROP TABLE llm_call_records")
    conn.commit()
    assert retention.prune_llm_call_records_step(conn, owner_id="owner", cutoff=1000, max_rows=8) == {
        "skipped": "table_absent"}
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='llm_call_records'").fetchone() is None


@pytest.mark.parametrize("kind", ["tools", "events", "llm"])
def test_select_count_is_constant_and_mutation_count_is_bounded(database, kind):
    _, _, conn = database
    _run(conn)
    for index in range(20):
        identity = str(index)
        if kind == "tools":
            _tool(conn, identity)
        elif kind == "events":
            # 每片同时包含 run 事件与 task-only 事件，覆盖所有批量状态查询。
            _event(conn, identity, run="run" if index % 2 else "")
        else:
            _llm(conn, identity)
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    counts = []
    for limit in (2, 8):
        statements.clear()
        if kind == "tools":
            result = retention.slim_terminal_tool_outcomes_step(conn, cutoff=1000, max_rows=limit)
            assert result["slimmed"] <= limit
        elif kind == "events":
            result = retention.prune_terminal_runtime_events_step(conn, cutoff=1000, max_rows=limit)
            assert result["deleted_events"] <= limit
        else:
            result = retention.prune_llm_call_records_step(conn, owner_id="owner", cutoff=1000, max_rows=limit)
            assert result["deleted_llm_records"] <= limit
        assert result["items"] <= limit
        counts.append(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements))
    assert counts == {"tools": [4, 4], "events": [5, 5], "llm": [2, 2]}[kind]
    conn.set_trace_callback(None)


@pytest.mark.parametrize("step", [retention.slim_terminal_tool_outcomes_step,
                                  retention.prune_terminal_runtime_events_step])
def test_failed_batch_rolls_back_both_mutations_and_cursor(database, step):
    _, _, conn = database
    _run(conn)
    original = _tool(conn)
    _event(conn, "event")
    conn.commit()
    conn.execute("CREATE TRIGGER reject_cursor BEFORE INSERT ON metadata "
                 "WHEN NEW.key LIKE 'history_retention_cursor:%' BEGIN SELECT RAISE(ABORT,'cursor rejected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="cursor rejected"):
        step(conn, cutoff=1000, max_rows=8)
    assert not conn.in_transaction
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original
    assert conn.execute("SELECT count(*) FROM runtime_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'history_retention_cursor:%'").fetchone()[0] == 0


def test_lane_missing_database_is_complete_without_creating_owner(tmp_path):
    home = home_paths(tmp_path / "missing")
    assert retention.maintain_runtime_history_lane(_request(home)) == {
        "cursor": {}, "complete": True, "items": 0, "bytes": 0}
    assert not home.owner_home_dir.exists()


def test_lane_uses_v2_policy_separate_cutoffs_and_owner_under_write_gate(database):
    home, _, conn = database
    now = 200 * 86400
    _run(conn)
    _tool(conn, "old-tool", created=now - 31 * 86400, settled=now - 31 * 86400)
    _tool(conn, "recent-tool", created=now - 29 * 86400, settled=now - 29 * 86400)
    _event(conn, "old-event", created=now - 181 * 86400)
    _event(conn, "recent-event", created=now - 179 * 86400)
    _llm(conn, "old-llm", owner=home.owner_id, created=now - 181 * 86400)
    _llm(conn, "recent-llm", owner=home.owner_id, created=now - 179 * 86400)
    _llm(conn, "other-owner", owner="other", created=now - 181 * 86400)
    conn.commit()
    commits = []

    def commit(operation):
        commits.append(True)
        return operation()

    receipt = retention.maintain_runtime_history_lane(_request(home, now=now, commit=commit))
    assert len(commits) == 3
    assert receipt["slimmed"] == receipt["deleted_events"] == receipt["deleted_llm_records"] == 1
    assert receipt["items"] == 5 and receipt["bytes"] > 0 and receipt["complete"]
    assert receipt["cursor"] == {"tool_operations": 1, "runtime_events": 1, "llm_call_records": ""}
    assert {row[0] for row in conn.execute("SELECT record_id FROM llm_call_records")} == {"recent-llm", "other-owner"}


@pytest.mark.parametrize("days", [0, -1])
def test_disabled_policy_days_do_not_run_steps(database, monkeypatch, days):
    from agent_py_agent.agent.memory_store import retention_scan

    home, _, conn = database
    _run(conn)
    original = _tool(conn)
    _event(conn, "event")
    _llm(conn, "llm", owner=home.owner_id)
    conn.commit()
    policy = replace(MemoryRetentionPolicy(), tool_output_days_after_terminal=days, audit_days=days)
    if days == 0:
        home.owner_retention_json.write_text(json.dumps(policy.to_dict()), encoding="utf-8")
    else:
        monkeypatch.setattr(retention_scan, "_load_policy", lambda path: (policy, None))

    def unexpected_write(operation):
        pytest.fail("关闭保留天数后仍进入写门")

    receipt = retention.maintain_runtime_history_lane(_request(home, commit=unexpected_write))
    assert receipt["complete"] and receipt["items"] == receipt["slimmed"] == 0
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original
    assert conn.execute("SELECT count(*) FROM runtime_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM llm_call_records").fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["request_hold", "policy_hold", "maintenance_disabled", "bad_policy"])
def test_lane_protection_and_unreadable_policy_do_not_mutate(database, reason):
    home, _, conn = database
    _run(conn)
    original = _tool(conn)
    conn.commit()
    policy = MemoryRetentionPolicy()
    if reason == "policy_hold":
        policy = replace(policy, legal_hold=True)
    elif reason == "maintenance_disabled":
        policy = replace(policy, maintenance_enabled=False)
    home.owner_retention_json.write_text("{" if reason == "bad_policy" else json.dumps(policy.to_dict()), encoding="utf-8")
    result = retention.maintain_runtime_history_lane(_request(home, legal_hold=reason == "request_hold"))
    assert result["items"] == 0
    assert result["complete"] is (reason != "bad_policy")
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original


@pytest.mark.parametrize("items,expired", [(0, False), (10, True)])
def test_lane_exhausted_budget_preserves_rows_and_persistent_cursor(database, items, expired):
    home, _, conn = database
    _run(conn)
    original = _tool(conn)
    conn.execute("INSERT INTO metadata(key,value) VALUES('history_retention_cursor:tool_operations','7')")
    conn.commit()
    result = retention.maintain_runtime_history_lane(_request(
        home, items=items, deadline=time.monotonic() - 1 if expired else None, cursor={"tool_operations": 99}))
    assert not result["complete"] and result["items"] == 0 and result["cursor"]["tool_operations"] == 7
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original


def test_lane_checks_time_between_steps_and_keeps_committed_progress(database):
    home, _, conn = database
    _run(conn)
    _tool(conn)
    _event(conn, "event")
    conn.commit()
    request = _request(home, deadline=time.monotonic() + 60)

    def commit(operation):
        result = operation()
        object.__setattr__(request.budget, "deadline", time.monotonic() - 1)
        return result

    request = replace(request, commit=commit)
    result = retention.maintain_runtime_history_lane(request)
    assert not result["complete"] and result["slimmed"] == 1 and result["deleted_events"] == 0
    assert conn.execute("SELECT count(*) FROM runtime_events").fetchone()[0] == 1


def test_lane_total_items_respect_request_and_retention_scan_slice_limit(database):
    from agent_py_agent.agent.memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES

    home, _, conn = database
    _run(conn)
    for index in range(150):
        _tool(conn, str(index))
        _event(conn, str(index))
        _llm(conn, str(index), owner=home.owner_id)
    conn.commit()
    for limit in (6, 1000):
        result = retention.maintain_runtime_history_lane(_request(home, items=limit))
        assert result["items"] <= min(limit, RETENTION_SCAN_SLICE_ENTRIES)
        assert sum(result[key] for key in ("slimmed", "deleted_events", "deleted_llm_records")) <= result["items"]
        assert all(result[key] > 0 for key in ("slimmed", "deleted_events", "deleted_llm_records"))
        assert not result["complete"]


def test_lane_does_not_install_an_absent_llm_table(database):
    home, _, conn = database
    conn.execute("DROP TABLE llm_call_records")
    conn.commit()
    result = retention.maintain_runtime_history_lane(_request(home))
    assert result["complete"] and result["deleted_llm_records"] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='llm_call_records'").fetchone() is None


def test_lane_rechecks_run_status_after_waiting_for_write_gate(database):
    home, repo, conn = database
    _run(conn)
    original = _tool(conn)
    conn.commit()
    entered, proceed = threading.Event(), threading.Event()
    results, errors = [], []

    def commit(operation):
        entered.set()
        assert proceed.wait(5)
        return operation()

    def worker():
        try:
            results.append(retention.maintain_runtime_history_lane(_request(home, commit=commit)))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        with repo.transaction() as writer:
            writer.execute("UPDATE agent_runs SET status='running' WHERE agent_run_id='run'")
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert results[0]["slimmed"] == 0
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original


@pytest.mark.parametrize("changed_column,changed_value", [("status", "UNKNOWN"), ("updated_at", 999)])
def test_tool_cas_does_not_replace_a_changed_row(database, changed_column, changed_value):
    _, _, conn = database
    _run(conn)
    original = _tool(conn)
    conn.commit()

    class ChangedConnection:
        """在读原文后、CAS 前注入确定性交错，模拟旧行快照不再匹配。"""

        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("UPDATE tool_operations SET outcome_json="):
                conn.execute(f"UPDATE tool_operations SET {changed_column}=? WHERE operation_id='op'", (changed_value,))
            return conn.execute(sql, parameters)

    result = retention.slim_terminal_tool_outcomes_step(ChangedConnection(), cutoff=1000, max_rows=8)
    assert result["slimmed"] == result["bytes"] == 0
    assert conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0] == original
