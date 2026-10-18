# LLM: 独立事务边界回归，受控异常类型不代真实物理退出证明。
# 模块用途: 核验原SQL/FD回收及已写正文和事件的真实partial。
import json
import sqlite3

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.tests.test_local_store_coordinator_io import make_store, read_sql, run_operation


@pytest.mark.parametrize("kind", ["bounded", "common"])
def test_actual_sql_cleanup_keeps_outer_and_event_body_partial(tmp_path, monkeypatch, kind):
    store, door = make_store(tmp_path)
    outer = (
        BoundedCallExitUnconfirmedError("review-type-only", "worker_exit_unconfirmed")
        if kind == "bounded"
        else GlobalWriteCommitUnavailable("review-scope-only")
    )
    cause = OSError("original cause")
    context = LookupError("original context")
    connections = []
    original_open = store._connect_coordinator

    def open_connection():
        conn = original_open()
        connections.append(conn)
        return conn

    original_append = store._append_coordinator_event_records

    def append_then_error(*args, **kwargs):
        original_append(*args, **kwargs)
        try:
            raise context
        except LookupError:
            raise outer from cause

    monkeypatch.setattr(store, "_connect_coordinator", open_connection)
    monkeypatch.setattr(store, "_append_coordinator_event_records", append_then_error)
    with global_write_commit_scope(door), pytest.raises(type(outer)) as caught:
        run_operation(store, "upsert")
    assert caught.value is outer and outer.__cause__ is cause and outer.__context__ is context
    assert connections
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    assert read_sql(store, "SELECT id FROM records") == []
    assert (
        json.loads(store.events_path.read_text().splitlines()[-1])["event_type"]
        == "record_upserted"
    )
    assert any(p.read_text() == "新的长期记忆正文" for p in store.files_dir.glob("*.txt"))
