"""用真实调用捕获 Q1–Q5 的 SQL，守护热查询索引与存量库自动补建。"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db import schema
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import (
    RuntimeRepository,
    _uncertain_attempt_mutation_ids,
)

_GROWING_TABLES = (
    "runtime_events", "tool_operations", "resource_mutations",
    "agent_attempts", "agent_runs", "task_runs",
)
_NEW_INDEXES = {
    "idx_resource_mutations_attempt": ("resource_mutations", ("attempt_id", "state")),
    "idx_tool_operations_run_status": ("tool_operations", ("agent_run_id", "status")),
}
_HOT_QUERIES = (
    ("Q1-all-events", "SEARCH runtime_events USING INDEX idx_runtime_events_agent_run_seq"),
    ("Q1-event-type", "SEARCH runtime_events USING INDEX idx_runtime_events_agent_run_seq"),
    ("Q2-event-bounds", "SEARCH runtime_events"),
    ("Q3-supersede", "SEARCH resource_mutations USING INDEX idx_resource_mutations_attempt"),
    ("Q3-terminal", "SEARCH resource_mutations USING INDEX idx_resource_mutations_attempt"),
    ("Q4-uncertain", "SEARCH resource_mutations USING INDEX idx_resource_mutations_attempt"),
    ("Q5-claim", "idx_tool_operations_run_status (agent_run_id=? AND status=?)"),
)


def _require(condition, reason):
    if not condition:
        pytest.skip(reason)


def _capture_hot_query(repo, monkeypatch, query_id):
    """只给真实连接装 trace；SQL 来自产品函数，捕获不到目标语句就失败。"""
    statements = []
    connect = repo._runtime_connect

    def traced_connect():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(repo, "_runtime_connect", traced_connect)
        if query_id.startswith("Q1-"):
            repo.events_for_agent_run("plan-agent", event_type=(
                "plan.event" if query_id == "Q1-event-type" else ""
            ), limit=10)
            prefix = "SELECT * FROM (SELECT * FROM runtime_events"
        elif query_id == "Q2-event-bounds":
            service = BusinessObservationService(repo.db_path.parent, owner_id="plan-owner", runtime=repo)
            with repo._runtime_connection() as conn:
                service._event_bounds(conn)
            prefix = "SELECT"
        elif query_id == "Q3-supersede":
            with repo.transaction() as conn:
                repo._supersede_noncurrent_attempts_conn(
                    conn, agent_run_id="plan-agent", current_attempt_id="plan-current",
                    current_generation=2, task_run_id="plan-task-run", now=100.0,
                    reason="plan-test", extra_cleanup_attempt_ids=("plan-old",),
                )
            prefix = "UPDATE resource_mutations"
        elif query_id == "Q4-uncertain":
            with repo._runtime_connection() as conn:
                _uncertain_attempt_mutation_ids(conn, "plan-old")
            prefix = "SELECT mutation_id FROM resource_mutations"
        else:
            chain = repo.record_run_creation(owner_id="plan-owner", goal="查询计划回归", run_id="plan-run")
            if query_id == "Q3-terminal":
                settled = repo.settle_agent_attempt(
                    agent_run_id=chain["agent_run_id"], attempt_id=chain["attempt_id"],
                )
                assert settled["settled"] is True
                statements.clear()
                with repo.transaction() as conn:
                    repo._release_terminal_attempt_resources_conn(
                        conn, chain["attempt_id"], reason="plan-test", now=100.0,
                    )
                prefix = "UPDATE resource_mutations"
            else:
                assert query_id == "Q5-claim"
                statements.clear()
                claimed = ManagedOperationStore(repo).claim_tool_operation(ToolOperationClaimRequest(
                    owner_id="plan-owner", run_id="plan-run", task_id="", operation_id="plan-operation",
                    tool="plan-tool", args_hash="plan-args", idempotency_key="plan-key",
                    idempotency_scope="operation", idempotency_namespace="plan-namespace",
                    holder=new_tool_operation_holder(), lease_expires_at=time.time() + 60,
                    attempt_id=chain["attempt_id"],
                ))
                assert claimed.action == "execute"
                prefix = "SELECT * FROM tool_operations WHERE agent_run_id="

    matches = [sql for sql in statements
        if " ".join(sql.split()).startswith(prefix)
        and (query_id != "Q2-event-bounds" or "FROM runtime_events" in sql)
        and (query_id != "Q5-claim" or re.search(r"\bstatus\s+IN\s*\(", sql, re.IGNORECASE))]
    assert len(matches) == 1, (query_id, statements)
    return matches[0]


@pytest.fixture(params=_HOT_QUERIES, ids=[item[0] for item in _HOT_QUERIES])
def hot_query(request, tmp_path, monkeypatch):
    query_id, required_plan = request.param
    if query_id == "Q2-event-bounds":
        # 以 R1 随片回归文件作为依赖已交付的事实，绝不按计划是否失败决定跳过。
        _require(
            Path(__file__).with_name("test_business_observability_event_bounds.py").is_file(),
            "R1 尚未合入：缺少其 event_bounds 回归文件；集成 R1 后自动启用 Q2 查询计划守护。",
        )
    repo = RuntimeRepository(tmp_path / "runtime.db")
    sql = _capture_hot_query(repo, monkeypatch, query_id)
    with sqlite3.connect(repo.db_path) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    return query_id, required_plan, plan


def test_hot_queries_use_bounded_index_plans(hot_query):
    query_id, required_plan, plan = hot_query
    assert any(required_plan in detail for detail in plan), (query_id, plan)
    if query_id == "Q2-event-bounds":
        assert sum("SEARCH runtime_events" in detail for detail in plan) == 2, plan
    # 子查询窗口的 SCAN/TEMP B-TREE 可以有；增长表本身（包括覆盖索引）不得全扫。
    for table in _GROWING_TABLES:
        pattern = rf"\bSCAN\s+(?:TABLE\s+)?(?:main\.)?{re.escape(table)}\b"
        assert not any(re.search(pattern, detail, re.IGNORECASE) for detail in plan), (query_id, table, plan)


def test_schema_fingerprint_installs_both_indexes_on_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    legacy = tuple(sql for sql in schema._BASE_RUNTIME_SQL if not any(name in sql for name in _NEW_INDEXES))
    assert len(legacy) == len(schema._BASE_RUNTIME_SQL) - len(_NEW_INDEXES)
    with monkeypatch.context() as patch:
        patch.setattr(schema, "_BASE_RUNTIME_SQL", legacy)
        old_repo = RuntimeRepository(path)
        survivor = old_repo.create_task(owner_id="plan-owner", title="升级保留")
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?, ?)", tuple(_NEW_INDEXES),
        ).fetchall()
        old_fingerprint = conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0]

    repo = RuntimeRepository(path)
    assert repo.get_task(survivor["task_id"])["title"] == "升级保留"
    with sqlite3.connect(path) as conn:
        for name, (table, columns) in _NEW_INDEXES.items():
            assert name in {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
            assert tuple(row[2] for row in conn.execute(f"PRAGMA index_info({name})")) == columns
        assert conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0] != old_fingerprint
        assert repo._install_runtime_schema(conn) is False
