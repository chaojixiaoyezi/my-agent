"""启动时孤儿进程检测钉子(REFACTORING_BACKLOG"启动时孤儿进程检测",孤儿回收
第二期:出口回收只覆盖结构化退出,异常崩溃路径靠开机巡检兜底)。

钉死契约:
1. 孤儿=任务已终态 + 落盘 pid 进程仍活 + cmdline 含本系统特征(三条全满足才报)。
2. 防误杀三连:已死 pid 不报 / cmdline 不匹配(pid 复用成别人进程)不报 /
   非终态任务的活进程(可能在干活)不报。
3. observability 先行:只报告进 ActiveWorkSummary 与文案,绝不自动 kill。
4. 防御:store 读取异常记 detection_errors 不崩。
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.startup_recovery import (
    ActiveWorkSummary,
    _detect_orphan_processes,
    format_active_work_summary,
    has_active_work,
)

pytestmark = pytest.mark.integration


def _spawn_marked_sleeper() -> subprocess.Popen:
    """cmdline 含 subagents-dispatch 特征的真实存活进程(身份验证可过)。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "subagents-dispatch-test-marker"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def _task(status: str, pid: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"sub-{pid}",
        status=status,
        attributes={"background_start": {"launch_id": "L1", "status": "running", "pid": pid}},
    )


def _agent(tasks: list) -> SimpleNamespace:
    return SimpleNamespace(subagents=SimpleNamespace(list_runs=lambda: tasks))


def test_terminal_task_with_live_marked_process_is_reported() -> None:
    proc = _spawn_marked_sleeper()
    try:
        summary = ActiveWorkSummary()
        _detect_orphan_processes(_agent([_task("DONE", proc.pid)]), summary)

        assert len(summary.orphan_processes) == 1
        orphan = summary.orphan_processes[0]
        assert orphan["pid"] == proc.pid
        assert orphan["task_status"] == "DONE"
        assert proc.poll() is None, "observability 先行:检测绝不杀进程"
        assert has_active_work(summary) is True
        assert "孤儿派工进程" in format_active_work_summary(summary)
    finally:
        _reap(proc)


def test_dead_pid_not_reported() -> None:
    proc = _spawn_marked_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    summary = ActiveWorkSummary()
    _detect_orphan_processes(_agent([_task("DONE", proc.pid)]), summary)
    assert summary.orphan_processes == []


def test_unmarked_process_not_reported_pid_reuse_guard() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        summary = ActiveWorkSummary()
        _detect_orphan_processes(_agent([_task("DONE", proc.pid)]), summary)
        assert summary.orphan_processes == [], "cmdline 无本系统特征=pid 复用,不得误报"
    finally:
        _reap(proc)


def test_non_terminal_task_live_process_not_reported() -> None:
    proc = _spawn_marked_sleeper()
    try:
        summary = ActiveWorkSummary()
        _detect_orphan_processes(_agent([_task("RUNNING", proc.pid)]), summary)
        assert summary.orphan_processes == [], "非终态任务的活进程可能在干活,不报"
    finally:
        _reap(proc)


def test_store_failure_recorded_not_raised() -> None:
    def boom():
        raise RuntimeError("store broken")

    summary = ActiveWorkSummary()
    _detect_orphan_processes(SimpleNamespace(subagents=SimpleNamespace(list_runs=boom)), summary)
    assert summary.orphan_processes == []
    assert summary.detection_errors[0]["context"] == "startup_recovery.orphan_processes"
