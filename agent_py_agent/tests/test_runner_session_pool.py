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
