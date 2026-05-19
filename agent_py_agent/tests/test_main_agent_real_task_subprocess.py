"""Focused tests for the streamed real-task subprocess runner."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
    RealTaskSubprocessRequest,
    run_real_task_subprocess,
)
from agent_py_agent.agent.contracts.main_agent_real_task_execution import _activity_timeout_seconds


# LLM: _request builds a tiny subprocess contract for live-log runner tests.
# 函数用途: 创建真实任务 subprocess request，隔离 stdout/stderr 和 workspace 路径。
def _request(tmp_path: Path, code: str, *, timeout: int = 5, activity_timeout: int = 0):
    return RealTaskSubprocessRequest(
        command=[sys.executable, "-c", code],
        cwd=tmp_path,
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        workspace=tmp_path / "workspace",
        timeout_seconds=timeout,
        activity_timeout_seconds=activity_timeout,
    )


# LLM: The subprocess runner should write logs while the process is still under control.
# 函数用途: 验证 stdout/stderr 不再等进程结束后才一次性写入，真实 E2E 运行中可观察。
def test_real_task_subprocess_streams_output_to_files(tmp_path):
    result = run_real_task_subprocess(
        _request(tmp_path, "print('hello-stream')", timeout=5)
    )

    assert result.exit_code == 0
    assert result.timed_out is False
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8").strip() == "hello-stream"


# LLM: Activity timeout is a machine liveness contract based on files, not prompt wording.
# 函数用途: 验证真实任务长时间没有日志或工作区写入时，会提前用 activity_timeout 停止。
def test_real_task_subprocess_activity_timeout_stops_idle_process(tmp_path):
    code = "import time; print('start', flush=True); time.sleep(10)"
    result = run_real_task_subprocess(
        _request(tmp_path, code, timeout=20, activity_timeout=2)
    )

    assert result.exit_code == 124
    assert result.timed_out is True
    assert result.timeout_reason == "activity_timeout"
    assert "start" in (tmp_path / "stdout.txt").read_text(encoding="utf-8")


# LLM: The first model call can be quiet before its first token, so idle timeout starts after observed progress.
# 函数用途: 验证真实任务首个可观察活动前不会被 activity_timeout 提前杀掉；总超时仍负责兜底。
def test_real_task_subprocess_waits_for_initial_activity_before_idle_timeout(tmp_path):
    code = "import time; time.sleep(1.4)"
    result = run_real_task_subprocess(
        _request(tmp_path, code, timeout=4, activity_timeout=1)
    )

    assert result.exit_code == 0
    assert result.timed_out is False


# LLM: Real model calls can be quiet after a tool result, so short task E2E uses total timeout as the idle floor.
# 函数用途: 验证真实任务 runner 的自动 activity timeout 不会比总超时短，避免慢模型首 token 被误杀。
def test_real_task_activity_timeout_uses_total_timeout_for_short_cases():
    assert _activity_timeout_seconds(240) == 240
    assert _activity_timeout_seconds(300) == 300
