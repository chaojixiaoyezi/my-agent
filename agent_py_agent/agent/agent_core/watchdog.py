
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class DispatchWatchdog:

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

    def start(self) -> None:
        if not self.enabled:
            return

        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_daemon()
            except Exception as e:
                # 记录错误但继续运行
                self._log(f"watchdog check error: {e}")

            self._stop_event.wait(self.interval)

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

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[Watchdog {timestamp}] {message}")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def start_watchdog(config: AgentConfig) -> DispatchWatchdog | None:
    watchdog = DispatchWatchdog(config)
    if watchdog.enabled:
        watchdog.start()
        return watchdog
    return None


__all__ = ["DispatchWatchdog", "start_watchdog"]


def _primary_workspace_root(raw_root: object) -> Path:
    if isinstance(raw_root, list):
        raw_root = raw_root[0] if raw_root else "."
    return Path(str(raw_root or ".")).resolve()


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
