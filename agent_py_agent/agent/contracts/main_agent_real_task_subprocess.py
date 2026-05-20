# LLM: Real task subprocess runner streams logs and reports structured timeout facts.
# 模块用途: 受控运行真实主代理任务，把 stdout/stderr 实时落盘，并用总超时/活动超时停止卡住进程。

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# LLM: RealTaskSubprocessRequest bundles the process contract for one case run.
# 类用途: 描述要启动的命令、工作目录、日志文件、任务工作区和超时边界。
@dataclass(frozen=True)
class RealTaskSubprocessRequest:
    command: list[str]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    workspace: Path
    timeout_seconds: int
    activity_timeout_seconds: int


# LLM: RealTaskSubprocessResult is the structured process outcome consumed by execution reports.
# 类用途: 保存进程退出码、耗时和超时类型；日志正文只通过文件引用读取。
@dataclass(frozen=True)
class RealTaskSubprocessResult:
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    timeout_reason: str = ""


# LLM: ProcessTimingSnapshot groups current timing facts for timeout decisions.
# 类用途: 保存进程开始、当前和最近活动时间，避免超时判断函数参数继续膨胀。
@dataclass(frozen=True)
class ProcessTimingSnapshot:
    now: float
    started: float
    last_activity: float
    observed_activity: bool = False


# LLM: run_real_task_subprocess executes without shell and keeps logs observable while the task runs.
# 函数用途: 启动真实任务子进程，把输出写入文件，并在总超时或长时间无活动时终止进程组。
def run_real_task_subprocess(request: RealTaskSubprocessRequest) -> RealTaskSubprocessResult:
    started = time.monotonic()
    request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    request.stderr_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["MY_AGENT_TOOL_DEADLINE_UNIX"] = f"{time.time() + max(1, request.timeout_seconds):.3f}"
    env.setdefault("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "10")
    with request.stdout_path.open("w", encoding="utf-8") as stdout_handle:
        with request.stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process = subprocess.Popen(
                request.command,
                cwd=request.cwd,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            return _wait_for_process(process, request, started)


# LLM: _wait_for_process polls process state and workspace activity without reading prompt prose.
# 函数用途: 根据进程状态、日志文件大小和工作区文件 mtime 判断是否完成、总超时或无活动超时。
def _wait_for_process(
    process: subprocess.Popen,
    request: RealTaskSubprocessRequest,
    started: float,
) -> RealTaskSubprocessResult:
    last_activity = time.monotonic()
    last_marker = _activity_marker(request)
    observed_activity = False
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return _process_result(exit_code, started)
        now = time.monotonic()
        marker = _activity_marker(request)
        if marker != last_marker:
            last_marker = marker
            last_activity = now
            observed_activity = True
        timeout_reason = _timeout_reason(
            ProcessTimingSnapshot(
                now=now,
                started=started,
                last_activity=last_activity,
                observed_activity=observed_activity,
            ),
            request,
        )
        if timeout_reason:
            _terminate_process_tree(process)
            return _process_result(124, started, timed_out=True, timeout_reason=timeout_reason)
        time.sleep(1.0)


# LLM: _timeout_reason returns stable machine issue codes for long-running process limits.
# 函数用途: 区分总时间到期和无活动到期，报告里不需要解析自然语言输出。
def _timeout_reason(
    timing: ProcessTimingSnapshot,
    request: RealTaskSubprocessRequest,
) -> str:
    if timing.now - timing.started >= max(1, request.timeout_seconds):
        return "timeout"
    if (
        timing.observed_activity
        and request.activity_timeout_seconds > 0
        and timing.now - timing.last_activity >= request.activity_timeout_seconds
    ):
        return "activity_timeout"
    return ""


# LLM: _activity_marker summarizes process-visible file progress without loading large artifacts.
# 函数用途: 用 stdout/stderr 大小和工作区最新 mtime 判断任务有没有继续活动。
def _activity_marker(request: RealTaskSubprocessRequest) -> tuple[int, int, float]:
    return (
        _file_size(request.stdout_path),
        _file_size(request.stderr_path),
        _latest_workspace_mtime(request.workspace),
    )


# LLM: _latest_workspace_mtime scans metadata only, so large outputs do not enter model context.
# 函数用途: 获取任务工作区最近文件修改时间，作为活动探针的一部分。
def _latest_workspace_mtime(root: Path) -> float:
    latest = 0.0
    try:
        for path in _workspace_files(root):
            latest = max(latest, path.stat().st_mtime)
    except OSError:
        return latest
    return latest


# LLM: _workspace_files hides filesystem traversal so activity mtime stays shallow.
# 函数用途: 迭代工作区文件；调用方只处理文件元数据，不进入额外嵌套判断。
def _workspace_files(root: Path):
    return (path for path in root.rglob("*") if path.is_file())


# LLM: _file_size reads file metadata only for activity monitoring.
# 函数用途: 返回日志文件大小；文件不存在时视为 0。
def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# LLM: _terminate_process_tree stops the whole process group started for one real task.
# 函数用途: 活动超时或总超时时终止主进程及其子进程，避免后台继续写同一任务目录。
def _terminate_process_tree(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return


# LLM: _process_result computes duration once and keeps the result payload compact.
# 函数用途: 构造进程结果对象，供真实任务执行报告继续做产物验收。
def _process_result(
    exit_code: int,
    started: float,
    *,
    timed_out: bool = False,
    timeout_reason: str = "",
) -> RealTaskSubprocessResult:
    return RealTaskSubprocessResult(
        exit_code=exit_code,
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
        timeout_reason=timeout_reason,
    )


__all__ = [
    "RealTaskSubprocessRequest",
    "RealTaskSubprocessResult",
    "run_real_task_subprocess",
]
