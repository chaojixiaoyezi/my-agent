"""显式 status 对失联 runner 的只读诊断测试。"""

from __future__ import annotations

import os
import subprocess
import time
from types import SimpleNamespace

from agent_py_agent.agent.startup_recovery import (
    ActiveWorkSummary,
    _detect_crashed_running_tasks,
    format_active_work_summary,
)


def _dead_pid() -> int:
    proc = subprocess.Popen(["sleep", "30"])
    proc.kill()
    proc.wait()
    return proc.pid


class _FakeTask:
    def __init__(
        self,
        run_id: str,
        status: str,
        worker_pid: int,
        *,
        fresh: bool = False,
        in_process: bool = False,
        background_pid: int = 0,
    ) -> None:
        self.id = run_id
        self.status = status
        heartbeat_at = time.time() if fresh else time.time() - 120
        self.attributes = {
            "runner_session": {
                "session_id": f"session-{run_id}",
                "status": "running",
                "worker_pid": worker_pid,
                "heartbeat_at": heartbeat_at,
                "interval_seconds": 5,
                "in_process": in_process,
            },
            "background_start": {"pid": background_pid},
        }


class _FakeSubagents:
    def __init__(self, tasks: list[object]) -> None:
        self._tasks = tasks

    def list_runs(self) -> list[object]:
        return self._tasks


def _agent(tasks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(subagents=_FakeSubagents(tasks))


def test_detects_only_stale_running_session_with_dead_host() -> None:
    dead = _dead_pid()
    tasks = [
        _FakeTask("crashed-1", "RUNNING", dead),
        _FakeTask("fresh-1", "RUNNING", os.getpid(), fresh=True),
        _FakeTask("blocked-1", "BLOCKED", dead),
        _FakeTask("done-1", "DONE", dead),
    ]
    summary = ActiveWorkSummary(gateway_pid=os.getpid())

    _detect_crashed_running_tasks(_agent(tasks), summary)

    assert [item["run_id"] for item in summary.crashed_tasks] == ["crashed-1"]
    assert tasks[0].status == "RUNNING"


def test_dead_background_dispatch_pid_does_not_override_live_runner_session() -> None:
    task = _FakeTask(
        "live-runner",
        "RUNNING",
        os.getpid(),
        fresh=True,
        background_pid=_dead_pid(),
    )
    summary = ActiveWorkSummary(gateway_pid=os.getpid())

    _detect_crashed_running_tasks(_agent([task]), summary)

    assert summary.crashed_tasks == []


def test_old_in_process_gateway_generation_is_visible_even_if_pid_was_reused() -> None:
    task = _FakeTask(
        "old-generation",
        "RUNNING",
        os.getpid(),
        in_process=True,
    )
    summary = ActiveWorkSummary(gateway_pid=os.getpid() + 1)

    _detect_crashed_running_tasks(_agent([task]), summary)

    assert summary.crashed_tasks[0]["reason"] == "gateway_generation_changed"


def test_report_explains_gateway_owned_reconciliation() -> None:
    summary = ActiveWorkSummary()
    summary.crashed_tasks = [
        {
            "run_id": "r1",
            "pid": 123,
            "status": "RUNNING",
            "reason": "runner_process_exited",
        }
    ]

    text = format_active_work_summary(summary)

    assert "runner 会话已失联" in text
    assert "单 Gateway" in text
    assert "status 本身不会改任务状态" in text


def test_detection_resilient_to_bad_subagents() -> None:
    agent = SimpleNamespace(subagents=SimpleNamespace())
    summary = ActiveWorkSummary()

    _detect_crashed_running_tasks(agent, summary)

    assert summary.crashed_tasks == []
    assert len(summary.detection_errors) == 1
