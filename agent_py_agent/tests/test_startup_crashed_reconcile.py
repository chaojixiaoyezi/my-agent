"""审计 #18(B,medium/任务)真测:崩溃卡死任务检测 + opt-in 自动调和。

无人值守 OOM/断电后,run 进程死了但任务永久卡 RUNNING,原本既不释放也不告警(孤儿检测只覆盖
"终态任务+进程活着")。真起进程真 kill 拿到确死 pid,断言:非终态+死 pid 被检出、活进程/终态/无
pid 不误检;默认关时不动任务状态(尊重现有策略),开 startup_auto_reconcile_crashed_tasks 后
自动调和到 ABANDONED(只改状态不杀进程)。学 通道运行时 shouldMarkLost + grace。
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from agent_py_agent.agent.startup_recovery import (
    ActiveWorkSummary,
    _detect_crashed_running_tasks,
    _maybe_reconcile_crashed_tasks,
    format_active_work_summary,
)


def _dead_pid() -> int:
    proc = subprocess.Popen(["sleep", "30"])  # 真起一个进程
    proc.kill()
    proc.wait()  # 真 kill 并回收 → 这个 pid 现在确实死了
    return proc.pid


class _FakeTask:
    def __init__(self, run_id: str, status: str, pid: int) -> None:
        self.id = run_id
        self.status = status
        self.attributes = {"background_start": {"pid": pid}} if pid else {}


class _FakeSubagents:
    def __init__(self, tasks: list) -> None:
        self._tasks = tasks
        self.set_status_calls: list = []
        self.lifecycle = SimpleNamespace(set_status=self._set_status)

    def list_runs(self) -> list:
        return self._tasks

    def _set_status(self, run_id: str, status: str, *, result: str = "", failure_type: str = "", **_kw):
        self.set_status_calls.append((run_id, status))
        for task in self._tasks:
            if task.id == run_id:
                task.status = status
        return SimpleNamespace(id=run_id, status=status)


def _agent(tasks: list, *, reconcile: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        subagents=_FakeSubagents(tasks),
        config=SimpleNamespace(startup_auto_reconcile_crashed_tasks=reconcile),
    )


def test_detect_only_real_crashed_tasks() -> None:
    dead = _dead_pid()
    tasks = [
        _FakeTask("crashed-1", "RUNNING", dead),  # 非终态 + 死 pid → 崩溃
        _FakeTask("alive-1", "RUNNING", os.getpid()),  # 非终态 + 活 pid(本进程)→ 不算
        _FakeTask("done-1", "DONE", dead),  # 终态 → 不算
        _FakeTask("nopid-1", "RUNNING", 0),  # 无记录 pid → 不算(无法确认崩溃)
    ]
    summary = ActiveWorkSummary()
    _detect_crashed_running_tasks(_agent(tasks), summary)
    assert [c["run_id"] for c in summary.crashed_tasks] == ["crashed-1"]  # 只检出真崩溃


def test_reconcile_off_by_default_keeps_status() -> None:
    agent = _agent([_FakeTask("crashed-1", "RUNNING", _dead_pid())], reconcile=False)
    summary = ActiveWorkSummary()
    _detect_crashed_running_tasks(agent, summary)
    _maybe_reconcile_crashed_tasks(agent, summary)
    assert agent.subagents.set_status_calls == []  # 默认关:不动任务状态(现有策略不变)
    assert not summary.crashed_tasks[0].get("reconciled")


def test_reconcile_on_marks_abandoned() -> None:
    agent = _agent([_FakeTask("crashed-1", "RUNNING", _dead_pid())], reconcile=True)
    summary = ActiveWorkSummary()
    _detect_crashed_running_tasks(agent, summary)
    _maybe_reconcile_crashed_tasks(agent, summary)
    assert agent.subagents.set_status_calls == [("crashed-1", "ABANDONED")]  # 自动调和
    assert summary.crashed_tasks[0]["reconciled"] is True


def test_report_surfaces_crashed_tasks() -> None:
    summary = ActiveWorkSummary()
    summary.crashed_tasks = [{"run_id": "r1", "pid": 123, "status": "RUNNING"}]
    text = format_active_work_summary(summary)
    assert "崩溃卡死任务" in text and "r1" in text  # 原本不可见的崩溃任务现在被告警
    assert "startup_auto_reconcile_crashed_tasks" in text  # 提示可开自动调和


def test_detection_resilient_to_bad_subagents() -> None:
    agent = SimpleNamespace(subagents=SimpleNamespace(), config=SimpleNamespace())  # 无 list_runs
    summary = ActiveWorkSummary()
    _detect_crashed_running_tasks(agent, summary)  # 不抛
    assert summary.crashed_tasks == [] and len(summary.detection_errors) == 1
