from __future__ import annotations

import time
from pathlib import Path

from agent_py_agent.agent.agent_core.runner.session_pool import (
    RunnerSessionPoolLease,
    runner_session_lease,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_runner_session_lease_records_heartbeat_and_completion(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(
        goal="跑 worker",
        thought="记录进程级 runner session。",
        plan=["启动", "完成"],
    )
    compact_ledger = (
        task.agent_run_workspace_dir
        + "/memory_archive/compact_applies/ledger.jsonl"
    )

    with runner_session_lease(
        RunnerSessionPoolLease(
            manager=manager,
            run_id=task.id,
            worker_id="worker-1",
            interval_seconds=0.01,
        )
    ):
        time.sleep(0.03)

    loaded = manager.load(task.id)
    session = loaded.attributes["runner_session"]
    assert session["schema_version"] == "runner_session_pool.v1"
    assert session["worker_id"] == "worker-1"
    assert session["worker_pid"] > 0
    assert session["status"] == "completed"
    assert session["heartbeat_at"] >= session["started_at"]
    assert not Path(compact_ledger).exists()
    assert manager.list_runs()[0].attributes["runner_session"]["status"] == "completed"


def test_runner_session_heartbeat_stops_at_cancelled_canonical_state(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(
        goal="跑长 worker",
        thought="取消后停止心跳。",
        plan=["启动", "取消"],
    )

    with runner_session_lease(
        RunnerSessionPoolLease(
            manager=manager,
            run_id=task.id,
            worker_id="worker-cancelled",
            interval_seconds=0.01,
        )
    ):
        time.sleep(0.03)
        cancelled = manager.load(task.id)
        session = dict(cancelled.attributes["runner_session"])
        cancelled.status = "CANCELLED"
        cancelled.failure_type = "cancelled"
        cancelled.ended_at = time.time()
        session["status"] = "cancelled"
        session["ended_at"] = cancelled.ended_at
        cancelled.attributes["runner_session"] = session
        manager.save(cancelled)
        terminal_heartbeat = session["heartbeat_at"]
        time.sleep(0.05)

    loaded = manager.load(task.id)
    assert loaded.status == "CANCELLED"
    assert loaded.attributes["runner_session"]["status"] == "cancelled"
    assert loaded.attributes["runner_session"]["heartbeat_at"] == terminal_heartbeat

    stale = dict(session)
    stale["status"] = "running"
    assert (
        manager.save_runner_session(
            task.id,
            stale,
            now=time.time() + 100,
        )
        is False
    )
    assert manager.load(task.id).attributes["runner_session"]["status"] == "cancelled"
