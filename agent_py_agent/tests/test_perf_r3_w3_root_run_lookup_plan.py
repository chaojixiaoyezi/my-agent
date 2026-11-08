"""PERF-R3-W3: root main-run lookups by task must not scan the parent='' index.

Live evidence (2026-11-07, Gateway 73125, runtime.db 2.07 GB, 10,740 agent_runs, 7,883 roots):
the background planner re-checks recovery eligibility for every blocked wake every tick; SQLite
chose idx_agent_runs_parent for parent_agent_run_id='' and walked every root run (3.1 ms/task
-> 0.005 ms/task once the lookup goes through task_runs(task_id)).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


class _RecordingConn:
    def __init__(self, conn, statements):
        self._conn = conn
        self._statements = statements

    def execute(self, sql, params=()):
        self._statements.append((sql, tuple(params)))
        return self._conn.execute(sql, params)


def _plan(repo, sql, params):
    conn = repo._runtime_connect()
    try:
        return [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]
    finally:
        conn.close()


def _root_lookup(statements):
    return [(sql, params) for sql, params in statements if "FROM agent_runs ar" in sql and "role" in sql]


def _assert_task_first_plan(repo, statements):
    lookups = _root_lookup(statements)
    assert lookups, statements
    for sql, params in lookups:
        plan = _plan(repo, sql, params)
        assert not any("idx_agent_runs_parent" in step for step in plan), plan
        assert any("idx_task_runs_task" in step for step in plan), plan


def test_recovery_eligibility_lookup_goes_through_task_runs(repo):
    statements = []
    with repo._runtime_connection() as conn:
        repo._recovery_eligibility_conn(_RecordingConn(conn, statements), "task-perf-r3")
    _assert_task_first_plan(repo, statements)


def test_main_agent_run_for_task_goes_through_task_runs(repo, monkeypatch):
    statements = []
    original = repo._runtime_connection

    @contextmanager
    def recording():
        with original() as conn:
            yield _RecordingConn(conn, statements)

    monkeypatch.setattr(repo, "_runtime_connection", recording)
    assert repo.main_agent_run_for_task("task-perf-r3") is None
    _assert_task_first_plan(repo, statements)
