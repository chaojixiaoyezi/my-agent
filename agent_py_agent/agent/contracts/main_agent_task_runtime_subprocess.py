# LLM: Main-agent task runtime subprocess runner is shared by task and real_task wrappers.
# 模块用途: 统一受控子进程启动、日志落盘、总超时和活动超时，避免双轨复制。

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# LLM: TaskRuntimeSubprocessRequest is the process contract for one controlled case.
# 类用途: 描述命令、运行目录、日志文件、任务 workspace 和两个超时边界。
@dataclass(frozen=True)
class TaskRuntimeSubprocessRequest:
    command: list[str]
    cwd: Path
    stdout_path: Path
    stderr_path: Path
    workspace: Path
    timeout_seconds: int
    activity_timeout_seconds: int


# LLM: TaskRuntimeSubprocessResult keeps process outcomes refs-first and compact.
# 类用途: 保存退出码、耗时和超时类型；stdout/stderr 正文只通过文件引用读取。
@dataclass(frozen=True)
class TaskRuntimeSubprocessResult:
    exit_code: int
    duration_seconds: float
    timed_out: bool = False
    timeout_reason: str = ""


# LLM: ProcessTimingSnapshot groups timing facts for timeout checks.
# 类用途: 保存开始、当前、最近活动时间和是否观察到活动，避免判断函数参数膨胀。
@dataclass(frozen=True)
class ProcessTimingSnapshot:
    now: float
    started: float
    last_activity: float
    observed_activity: bool = False


# LLM: run_task_runtime_subprocess executes without shell and writes observable logs.
# 函数用途: 启动真实主代理任务进程，并在总超时或无活动超时时终止进程组。
def run_task_runtime_subprocess(request: TaskRuntimeSubprocessRequest) -> TaskRuntimeSubprocessResult:
    started = time.monotonic()
    request.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    request.stderr_path.parent.mkdir(parents=True, exist_ok=True)
    env = _subprocess_env(request.timeout_seconds)
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


# LLM: _subprocess_env propagates deadline facts to child tools without prompt text.
# 函数用途: 给子进程设置结构化 deadline 环境变量，工具层可据此提前停止。
def _subprocess_env(timeout_seconds: int) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["MY_AGENT_TOOL_DEADLINE_UNIX"] = f"{time.time() + max(1, timeout_seconds):.3f}"
    env.setdefault("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "10")
    return env


# LLM: _wait_for_process polls process state and workspace activity.
# 函数用途: 用进程退出、日志大小和工作区 mtime 判断完成、总超时或无活动超时。
def _wait_for_process(
    process: subprocess.Popen,
    request: TaskRuntimeSubprocessRequest,
    started: float,
) -> TaskRuntimeSubprocessResult:
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
            ProcessTimingSnapshot(now, started, last_activity, observed_activity),
            request,
        )
        if timeout_reason:
            _terminate_process_tree(process)
            return _process_result(124, started, timed_out=True, timeout_reason=timeout_reason)
        time.sleep(1.0)


# LLM: _timeout_reason returns stable machine issue codes for process limits.
# 函数用途: 区分总超时和活动超时，报告不需要解析自然语言输出。
def _timeout_reason(
    timing: ProcessTimingSnapshot,
    request: TaskRuntimeSubprocessRequest,
) -> str:
    if timing.now - timing.started >= max(1, request.timeout_seconds):
        return "timeout"
    inactive = timing.now - timing.last_activity >= request.activity_timeout_seconds
    if timing.observed_activity and request.activity_timeout_seconds > 0 and inactive:
        return "activity_timeout"
    return ""


# LLM: _activity_marker summarizes progress without loading large artifacts.
# 函数用途: 用 stdout/stderr 大小和 workspace 最新 mtime 作为轻量活动探针。
def _activity_marker(request: TaskRuntimeSubprocessRequest) -> tuple[int, int, float]:
    return (_file_size(request.stdout_path), _file_size(request.stderr_path), _latest_workspace_mtime(request.workspace))


# LLM: _latest_workspace_mtime scans metadata only.
# 函数用途: 读取工作区文件 mtime，不把文件内容塞进内存或模型上下文。
def _latest_workspace_mtime(root: Path) -> float:
    latest = 0.0
    try:
        for path in (item for item in root.rglob("*") if item.is_file()):
            latest = max(latest, path.stat().st_mtime)
    except OSError:
        return latest
    return latest


# LLM: _file_size reads file metadata only for activity monitoring.
# 函数用途: 返回日志文件大小；文件不存在或不可读时视为 0。
def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# LLM: _terminate_process_tree stops the whole process group for one case.
# 函数用途: 超时时先 TERM 后 KILL，避免后台子进程继续写同一任务目录。
def _terminate_process_tree(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            return


# LLM: _process_result computes duration once and keeps payload compact.
# 函数用途: 构造进程结果对象，供执行报告继续做产物验收。
def _process_result(
    exit_code: int,
    started: float,
    *,
    timed_out: bool = False,
    timeout_reason: str = "",
) -> TaskRuntimeSubprocessResult:
    return TaskRuntimeSubprocessResult(exit_code, time.monotonic() - started, timed_out, timeout_reason)


__all__ = [
    "TaskRuntimeSubprocessRequest",
    "TaskRuntimeSubprocessResult",
    "run_task_runtime_subprocess",
]
