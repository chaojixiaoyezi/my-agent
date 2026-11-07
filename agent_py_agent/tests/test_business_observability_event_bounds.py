"""事件边界保持原自增水位语义，并分别读取索引端点。"""

import sqlite3
from contextlib import closing

import pytest

from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.runtime_db.schema import _BASE_RUNTIME_SQL, RuntimeSchemaMixin

_ORIGINAL_EVENT_BOUNDS_SQL = (
    "SELECT min(seq),COALESCE(max(seq),"
    "(SELECT seq FROM sqlite_sequence WHERE name='runtime_events'),0) FROM runtime_events"
)


@pytest.fixture(params=["empty", "populated", "deleted"])
def event_database(tmp_path, request):
    with closing(sqlite3.connect(tmp_path / "runtime.db")) as conn:
        RuntimeSchemaMixin._execute_runtime_schema(conn, _BASE_RUNTIME_SQL)
        expected = (None, 0)
        if request.param != "empty":
            count = 128
            conn.executemany(
                "INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,created_at) "
                "VALUES(?,?,?,?,?)",
                [(f"event-{number}", "test.event", "attempt", "run", float(number))
                 for number in range(1, count + 1)],
            )
            expected = (1, count)
            if request.param == "deleted":
                conn.execute("DELETE FROM runtime_events")
                expected = (None, count)
        conn.commit()
        yield conn, expected


def test_event_bounds_matches_original_sql(tmp_path, event_database):
    conn, expected = event_database
    service = BusinessObservationService(tmp_path, owner_id="local/test")

    original = conn.execute(_ORIGINAL_EVENT_BOUNDS_SQL).fetchone()
    actual = service._event_bounds(conn)

    assert type(actual) is tuple
    assert actual == original == expected


def test_event_bounds_query_plan_avoids_runtime_event_scan(tmp_path, event_database):
    conn, _ = event_database
    service = BusinessObservationService(tmp_path, owner_id="local/test")
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        service._event_bounds(conn)
    finally:
        conn.set_trace_callback(None)

    assert len(statements) == 1
    plan = conn.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
    assert not any("SCAN RUNTIME_EVENTS" in row[3].upper() for row in plan), plan
