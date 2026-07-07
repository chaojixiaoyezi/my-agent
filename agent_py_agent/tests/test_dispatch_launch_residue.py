"""background_start 残留判活(P2 宿主级重启):launching/running 记录冻结不再永久挡续派。

真机实锤:网关被杀时 run 的 background_start 冻在 launching/running,重启后
runner_launch_in_progress 按盘上字符串永久排除该 run——supervision 的 PENDING 复活
扫描也被同一判定挡死,重新派发的子代理卡 PENDING 12 分钟不恢复。判据全结构化:
pid 存活 > runner 会话心跳 > 记录时效。
"""

from __future__ import annotations

import os
import subprocess
import time
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    _clear_background_start_residue,
)
from agent_py_agent.agent.agent_core.runner.dispatch import (
    RunnerCandidatePolicy,
    _is_dispatch_runner_candidate,
    runner_launch_in_progress,
)


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])  # noqa: S603,S607 - 立即退出的真实 pid
    proc.wait()
    return proc.pid


def _task(background: dict, *, status: str = "PENDING", session: dict | None = None) -> SimpleNamespace:
    attributes: dict = {"background_start": background}
    if session is not None:
        attributes["runner_session"] = session
    return SimpleNamespace(
        id="run-x",
        status=status,
        attributes=attributes,
        runner_active_attempt_id="",
        channel_status="",
        capability_requests=[],
        capability_gaps=[],
        verification_status="",
        failure_type="",
        runner_attempts=0,
    )


def _policy() -> RunnerCandidatePolicy:
    return RunnerCandidatePolicy()


def test_running_with_live_pid_still_blocks() -> None:
    task = _task({"status": "running", "pid": os.getpid(), "updated_at": time.time() - 9999})
    assert runner_launch_in_progress(task, _policy()) is True


def test_running_with_dead_pid_no_longer_blocks() -> None:
    task = _task({"status": "running", "pid": _dead_pid(), "updated_at": time.time()})
    assert runner_launch_in_progress(task, _policy()) is False
    assert _is_dispatch_runner_candidate(task) is True, "宿主已死的 PENDING 必须能被续派复活"


def test_fresh_launching_record_blocks() -> None:
    task = _task({"status": "launching", "updated_at": time.time() - 5})
    assert runner_launch_in_progress(task, _policy()) is True


def test_stale_launching_record_no_longer_blocks() -> None:
    task = _task({"status": "launching", "updated_at": time.time() - 600})
    assert runner_launch_in_progress(task, _policy()) is False
    assert _is_dispatch_runner_candidate(task) is True


def test_running_without_pid_with_fresh_session_blocks() -> None:
    # scoped 线程路(mark running 不带 pid):runner 会话心跳新鲜 = 真在跑。
    task = _task(
        {"status": "running", "updated_at": time.time() - 600},
        session={"status": "running", "heartbeat_at": time.time() - 2, "interval_seconds": 5},
    )
    assert runner_launch_in_progress(task, _policy()) is True


def test_running_without_pid_stale_record_no_longer_blocks() -> None:
    task = _task({"status": "running", "updated_at": time.time() - 600})
    assert runner_launch_in_progress(task, _policy()) is False


def test_legacy_record_without_updated_at_treated_as_residue() -> None:
    task = _task({"status": "launching"})
    assert runner_launch_in_progress(task, _policy()) is False


def test_requeue_clears_background_start_residue() -> None:
    # supervision 回收 RUNNING→PENDING 时把残留标成 reclaimed(pid 记录不丢),
    # requeue 出的 PENDING 不再被 launch_in_progress 按残留排除。
    task = _task({"status": "running", "pid": 12345, "launch_id": "L-1", "updated_at": time.time()})
    _clear_background_start_residue(task)
    background = task.attributes["background_start"]
    assert background["status"] == "reclaimed"
    assert background["pid"] == 12345
    assert background["launch_id"] == "L-1"
    assert runner_launch_in_progress(task, _policy()) is False
