"""W10: 历史增长、候选读取与写事务之间的安全边界。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


# LLM: 只在临时库批量准备历史，不调用任何生产进程。
# 函数用途: 固定活动集合，仅增长已结束历史。
def _seed(repo, count, active):
    with repo.transaction() as conn:
        conn.executemany(
            "INSERT INTO agent_runs(agent_run_id,task_run_id,status,current_attempt_id,created_at,updated_at) "
            "VALUES(?, 'task', 'done', ?, 1, 1)",
            ((f"old-{i:06}", f"attempt-{i:06}") for i in range(count)),
        )
        conn.executemany(
            "INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at) "
            "VALUES(?, ?, 1, 'done', 1)",
            ((f"attempt-{i:06}", f"old-{i:06}") for i in range(count)),
        )
        for i in range(active):
            conn.execute(
                "INSERT INTO agent_runs(agent_run_id,task_run_id,status,current_attempt_id,created_at,updated_at) "
                "VALUES(?, 'task', 'unknown', ?, 1, 1)", (f"live-{i}", f"live-at-{i}"),
            )
            conn.execute(
                "INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at) "
                "VALUES(?, ?, 1, 'unknown', 1)", (f"live-at-{i}", f"live-{i}"),
            )


# LLM: 仅观察真实SQLite语句、VM指令、解码行与持锁区间，不替换业务结果。
# 函数用途: 给所有原生连接安装有界成本计量。
def _meter(monkeypatch):
    original = RuntimeRepository._runtime_connect
    totals = dict(steps=0, locked_steps=0, rows=0, lock_seconds=0.0)
    queries = []

    def connect(self):
        conn = original(self)
        state = {"locked": False, "start": 0.0}

        def trace(sql):
            upper = sql.upper()
            if upper.startswith("BEGIN IMMEDIATE"):
                state.update(locked=True, start=time.monotonic())
            if upper.startswith(("COMMIT", "ROLLBACK")) and state["locked"]:
                totals["lock_seconds"] += time.monotonic() - state["start"]
                state["locked"] = False
            if sql.lstrip().startswith("SELECT") and "LIMIT" in upper and "agent_runs" in sql:
                queries.append(sql)

        def progress():
            totals["steps"] += 1
            totals["locked_steps"] += int(state["locked"])
            return 0

        def row(cursor, values):
            totals["rows"] += 1
            return sqlite3.Row(cursor, values)

        conn.set_trace_callback(trace)
        conn.set_progress_handler(progress, 1)
        conn.row_factory = row
        return conn

    monkeypatch.setattr(RuntimeRepository, "_runtime_connect", connect)
    return totals, queries


# LLM: 每个活动规模各测10k和100k，测量维护真实入口及重新构造后的入口。
# 函数用途: 防止终态历史进入候选扫描或写锁；同时核对结果、FD与执行计划。
@pytest.mark.parametrize("active", [0, 1, 8])
def test_maintenance_cost_independent_of_terminal_history(tmp_path, monkeypatch, active):
    repos = []
    for count in (10000, 100000):
        repo = RuntimeRepository(tmp_path / f"runtime-{count}.db")
        _seed(repo, count, active)
        repos.append(repo)
    totals, queries = _meter(monkeypatch)
    samples = []
    fd_before = len(os.listdir("/dev/fd"))
    for repo in repos:
        before = dict(totals)
        result = repo.cleanup_stale_runtime(now=50)
        assert result == {"recovered": [], "settled": [], "released_locks": []}
        samples.append({key: totals[key] - before[key] for key in totals})
    assert len(os.listdir("/dev/fd")) - fd_before == 0
    print("runtime_cost", json.dumps({"active": active, "samples": samples}, sort_keys=True))
    assert samples[1]["steps"] <= samples[0]["steps"] * 1.3 + 300
    assert samples[1]["locked_steps"] <= samples[0]["locked_steps"] * 1.3 + 100
    assert samples[1]["rows"] <= samples[0]["rows"] + 8
    assert samples[1]["locked_steps"] < 12000
    with repos[1]._runtime_connection() as conn:
        for query in set(queries):
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
            print("runtime_plan", plan)
            assert not any("SCAN ar" in part or "SCAN at" in part or "TEMP B-TREE" in part for part in plan)
    restarted = RuntimeRepository(repos[1].db_path)
    before = totals["steps"]
    assert restarted.cleanup_stale_runtime(now=50) == result
    assert totals["steps"] - before < 20000
    assert len(os.listdir("/dev/fd")) - fd_before == 0


# LLM: 真实线程只在候选查询完成后更新同一行，使用Event确定交错，不以时间碰运气。
# 函数用途: 确认读取阶段不持写锁，且稍后变活跃的attempt不被旧候选取消。
def test_candidate_read_does_not_hold_writer_and_rechecks_changed_attempt(tmp_path, monkeypatch):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    _seed(repo, 0, 1)
    original = repo._stale_runtime_rows
    ready, done = threading.Event(), threading.Event()
    failures = []

    def update():
        ready.wait(5)
        try:
            with repo.transaction() as conn:
                conn.execute("UPDATE agent_attempts SET status='running' WHERE attempt_id='live-at-0'")
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    def read(conn, **kwargs):
        rows = original(conn, **kwargs)
        ready.set()
        assert done.wait(2), "候选读取仍持有写锁，另一写者不能推进"
        return rows

    monkeypatch.setattr(repo, "_stale_runtime_rows", read)
    thread = threading.Thread(target=update)
    thread.start()
    try:
        assert repo.settle_unidentified_attempts(older_than_seconds=0, now=90000, automatic=True) == []
    finally:
        ready.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert repo.get_attempt("live-at-0")["status"] == "running"


# LLM: 游标页尾短页仍保留最后访问位置；空页才允许回到活动索引起点。
# 函数用途: 防止短页直接重头；并核对最终取消与幂等。
def test_short_page_keeps_cursor_and_orphan_settlement(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    _seed(repo, 0, 1)
    assert repo.settle_unidentified_attempts(older_than_seconds=0, now=50, automatic=True) == []
    assert repo._stale_settlement_cursor == (1.0, "live-0")
    with repo.transaction() as conn:
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,status,created_at,updated_at) "
                     "VALUES('orphan','task','created',2,2)")
    assert repo.cleanup_stale_runtime(now=4000)["settled"] == ["orphan"]
    assert repo.cleanup_stale_runtime(now=90000)["settled"] == ["live-0"]
    assert repo.cleanup_stale_runtime(now=90001)["settled"] == []


# LLM: 真实SimpleAgent/GatewayContext/controller执行维护拍，不替换清理；重启重新装配同一隔离home。
# 函数用途: 验证10万终态在Gateway首拍和重启后均不进入候选工作集。
def test_real_gateway_maintenance_tick_and_restart(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.cli.gateway_loops import _GatewayOwnerMaintenanceController
    from agent_py_agent.cli.models import GatewayRunContext

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False)
    agent = SimpleAgent(config, tmp_path)
    repo = agent.subagents.runtime_db
    _seed(repo, 100000, 0)
    totals, _queries = _meter(monkeypatch)
    measurements = []
    fd_before = len(os.listdir("/dev/fd"))
    for current in (agent, SimpleAgent(config, tmp_path)):
        context = GatewayRunContext(agent=current, paths=None, config_path=tmp_path / "config.yaml")
        controller = _GatewayOwnerMaintenanceController(context)
        before = totals["steps"]
        for _ in range(3):
            assert controller.tick()["failed"] == 0
        measurements.append(totals["steps"] - before)
        with current.subagents.runtime_db._runtime_connection() as conn:
            assert conn.execute("SELECT count(*) FROM agent_runs WHERE status='done'").fetchone()[0] == 100000
    assert all(0 < steps < 20000 for steps in measurements)
    assert len(os.listdir("/dev/fd")) - fd_before == 0
    print("gateway_tick_restart", measurements)
