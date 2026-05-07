# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


# LLM: DispatchWatchdog 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 封装调度watchdog相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class DispatchWatchdog:

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, config: AgentConfig):
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_count = 0

        # 配置
        self.enabled = getattr(config, "watchdog_enabled", False)
        self.interval = getattr(config, "watchdog_interval", 60)
        self.max_restarts = getattr(config, "watchdog_max_restarts", 3)
        self.restart_delay = getattr(config, "watchdog_restart_delay", 10)

        # PID 文件路径
        self.pid_file = _primary_workspace_root(config.workspace_root) / config.gateway_workspace / "gateway.pid"

    # LLM: start 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进start的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def start(self) -> None:
        if not self.enabled:
            return

        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # LLM: stop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进stop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # LLM: _run 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进run的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_daemon()
            except Exception as e:
                # 记录错误但继续运行
                self._log(f"watchdog check error: {e}")

            self._stop_event.wait(self.interval)

    # LLM: _check_daemon 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 校验守护进程需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _check_daemon(self) -> None:
        if not self.pid_file.exists():
            self._log("PID file not found, daemon may not be running")
            return

        try:
            pid_str = self.pid_file.read_text(encoding="utf-8").strip()
            pid = int(pid_str)
        except (OSError, ValueError) as e:
            self._log(f"Failed to read PID file: {e}")
            return

        # 检查进程是否存活
        if self._is_pid_alive(pid):
            self._log(f"Daemon is alive (pid={pid})")
            return

        # 进程已死
        self._log(f"Daemon process {pid} is dead")
        self._handle_dead_daemon(pid)

    # LLM: _is_pid_alive 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 判断pidalive条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            return _is_pid_alive_windows(pid)
        try:
            # 发送信号 0 不实际发送信号，只检查进程是否存在
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    # LLM: _handle_dead_daemon 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进dead守护进程的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _handle_dead_daemon(self, pid: int) -> None:
        if self._restart_count >= self.max_restarts:
            self._log(f"Max restarts ({self.max_restarts}) reached, not restarting")
            return

        self._log(f"Waiting {self.restart_delay}s before attempting restart...")
        time.sleep(self.restart_delay)

        if self._stop_event.is_set():
            return

        self._log("Attempting to restart daemon...")
        self._restart_daemon()

    # LLM: _restart_daemon 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进守护进程的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _restart_daemon(self) -> None:
        try:
            import subprocess

            # 构造重启命令
            # 使用 my-agent daemon 命令重启
            cmd = ["python", "-m", "agent_py_agent", "daemon"]

            # 启动新进程
            subprocess.Popen(
                cmd,
                cwd=str(_primary_workspace_root(self.config.workspace_root)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            self._restart_count += 1
            self._log(f"Daemon restarted, restart count: {self._restart_count}")

        except Exception as e:
            self._log(f"Failed to restart daemon: {e}")

    # LLM: _log 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 写入log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
    def _log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[Watchdog {timestamp}] {message}")

    # LLM: is_running 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 判断running条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# LLM: start_watchdog 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进watchdog的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def start_watchdog(config: AgentConfig) -> DispatchWatchdog | None:
    watchdog = DispatchWatchdog(config)
    if watchdog.enabled:
        watchdog.start()
        return watchdog
    return None


__all__ = ["DispatchWatchdog", "start_watchdog"]


# LLM: _primary_workspace_root 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理primaryworkspaceroot相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _primary_workspace_root(raw_root: object) -> Path:
    if isinstance(raw_root, list):
        raw_root = raw_root[0] if raw_root else "."
    return Path(str(raw_root or ".")).resolve()


# LLM: _is_pid_alive_windows 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断pidalivewindows条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - defensive
        return False

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
