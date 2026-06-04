
from __future__ import annotations

"""gateway watchdog supervisor — monitors gateway health and auto-restarts on crash.

这个文件实现一个独立的后台监督进程，负责监控 gateway 是否崩溃，并在崩溃后自动拉起。
不依赖 systemd，在 macOS/Linux 上都能跑。
"""

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    WriteRuntimeStatusParams,
    acquire_scoped_lock,
    get_running_pid,
    get_running_pid_report,
    read_pid_file,
    read_runtime_status,
    release_scoped_lock,
    remove_pid_file,
    write_pid_file,
    write_runtime_status,
)
from .paths import gateway_paths
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .supervisor_health import (
    SupervisorPayloadReport,
    read_adapter_state,
    read_adapter_state_report,
    read_gateway_heartbeat_report,
    record_health_load_error,
)
from .supervisor_runtime import (
    restart_gateway as _restart_gateway_impl,
)
from .supervisor_runtime import (
    run_supervisor_loop,
)
from .supervisor_runtime import (
    start_gateway as _start_gateway_impl,
)
from .supervisor_runtime import (
    stop_gateway as _stop_gateway_impl,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_read_adapter_state = read_adapter_state
_read_adapter_state_report = read_adapter_state_report


@dataclass
class SupervisorConfig:

    workspace_root: str | None = None
    heartbeat_timeout: float = 120.0
    check_interval: float = 10.0
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0
    log_path: Path | None = None


class GatewaySupervisor:

    def __init__(
        self,
        config_path: str,
        *,
        options: SupervisorConfig | None = None,
    ):
        self.config_path = config_path
        _opts = options or SupervisorConfig()
        self.workspace_root = _opts.workspace_root
        self.heartbeat_timeout = _opts.heartbeat_timeout
        self.check_interval = _opts.check_interval
        self.max_restart_attempts = _opts.max_restart_attempts
        self.restart_cooldown = _opts.restart_cooldown
        self.log_path = _opts.log_path

        self._supervisor_pid: int = os.getpid()
        self._gateway_pid: int | None = None
        self._restart_count: int = 0
        self._last_restart_at: float = 0.0
        self._stop_requested: bool = False
        self._agent = None
        self._paths = None
        self._last_health_load_errors: list[dict] = []

    def _resolve_agent_and_paths(self):
        if self._agent is not None or self._paths is not None:
            return

        # Import lazily to avoid circular dependencies
        from ..core import SimpleAgent
        from ..settings import load_config

        config = load_config(self.config_path)
        roots = _workspace_roots(config.workspace_root, Path(self.config_path).resolve().parent)
        root = roots[0]
        if self.workspace_root:
            root = Path(self.workspace_root).resolve()
            roots = [root]

        self._agent = SimpleAgent(config, root, workspace_roots=roots)
        self._paths = gateway_paths(self._agent)

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line, flush=True)
        self._append_log_file(line)

    def _append_log_file(self, line: str) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _log_info(self, msg: str) -> None:
        self._log("INFO", msg)

    def _log_warn(self, msg: str) -> None:
        self._log("WARN", msg)

    def _log_error(self, msg: str) -> None:
        self._log("ERROR", msg)

    def _read_gateway_heartbeat(self) -> dict | None:
        return self._read_gateway_heartbeat_report().payload

    def _read_gateway_heartbeat_report(self) -> SupervisorPayloadReport:
        self._resolve_agent_and_paths()
        return read_gateway_heartbeat_report(self._paths.heartbeat)

    def _is_gateway_healthy(self) -> bool:
        self._resolve_agent_and_paths()
        self._last_health_load_errors = []

        # Check heartbeat freshness first (most reliable)
        heartbeat_report = self._read_gateway_heartbeat_report()
        if heartbeat_report.load_error is not None:
            self._record_health_load_error(heartbeat_report.load_error)
            return False
        heartbeat = heartbeat_report.payload
        if self._heartbeat_is_fresh(heartbeat):
            return True

        # Secondary health source: check PID file directly.
        pid_report = get_running_pid_report(self._paths.pid)
        if pid_report.load_error is not None:
            self._record_health_load_error(pid_report.load_error)
            return False
        pid = pid_report.pid
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        # PID file is fresh but no heartbeat - could be startup phase
        if not heartbeat:
            self._log_info("Gateway PID alive but no heartbeat yet (startup phase)")
            return True

        return False

    def _record_health_load_error(self, load_error: dict) -> None:
        record_health_load_error(
            self._last_health_load_errors,
            load_error,
            log_warn=self._log_warn,
        )

    def _heartbeat_is_fresh(self, heartbeat: dict | None) -> bool:
        if not heartbeat:
            return False
        updated_at = heartbeat.get("updated_at", 0)
        if not updated_at:
            return False
        age = time.time() - float(updated_at)
        if age <= self.heartbeat_timeout:
            return True
        self._log_warn(f"Gateway heartbeat stale: age={age:.1f}s > {self.heartbeat_timeout}s")
        return False

    def _check_adapter_health(self) -> bool:
        self._resolve_agent_and_paths()

        # Check adapter PID file
        pid_report = get_running_pid_report(self._paths.adapter_pid)
        if pid_report.load_error is not None:
            self._record_health_load_error(pid_report.load_error)
            return False
        pid = pid_report.pid
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        state_path = self._paths.root / "adapter_state.json"
        state_report = _read_adapter_state_report(state_path)
        if state_report.load_error is not None:
            self._record_health_load_error(state_report.load_error)
            return False
        state = state_report.payload or {}
        if state.get("state") == "running":
            return True

        return True  # PID alive, assume healthy if no state file

    def _start_gateway(self) -> int | None:
        return _start_gateway_impl(self)

    def _stop_gateway(self, timeout: float = 20.0) -> bool:
        return _stop_gateway_impl(self, timeout=timeout)

    def _restart_gateway(self) -> bool:
        return _restart_gateway_impl(self)

    def _handle_signal(self, signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._log_info(f"Received {sig_name}, initiating graceful shutdown...")
        self._stop_requested = True

    def run(self) -> int:
        return run_supervisor_loop(self)


def run_supervisor(
    config_path: str,
    options: SupervisorConfig | None = None,
    *,
    workspace_root: str | None = None,
    heartbeat_timeout: float = 120.0,
    check_interval: float = 10.0,
    max_restart_attempts: int = 5,
    restart_cooldown: float = 30.0,
    log_path: Path | None = None,
) -> int:
    if options is None:
        options = SupervisorConfig(
            workspace_root=workspace_root,
            heartbeat_timeout=heartbeat_timeout,
            check_interval=check_interval,
            max_restart_attempts=max_restart_attempts,
            restart_cooldown=restart_cooldown,
            log_path=log_path,
        )
    supervisor = GatewaySupervisor(config_path, options=options)
    return supervisor.run()


def is_supervisor_running(config_path: str) -> bool:
    from ..core import SimpleAgent
    from ..settings import load_config

    config = load_config(config_path)
    roots = _workspace_roots(config.workspace_root, Path(config_path).resolve().parent)
    agent = SimpleAgent(config, roots[0], workspace_roots=roots)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    if not supervisor_pid_path.exists():
        return False

    pid = read_pid_file(supervisor_pid_path)
    if not pid:
        return False

    return is_pid_alive(pid)


def stop_supervisor(config_path: str, timeout: float = 10.0) -> bool:
    from ..core import SimpleAgent
    from ..settings import load_config

    config = load_config(config_path)
    roots = _workspace_roots(config.workspace_root, Path(config_path).resolve().parent)
    agent = SimpleAgent(config, roots[0], workspace_roots=roots)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    pid = read_pid_file(supervisor_pid_path)
    if not pid or not is_pid_alive(pid):
        return True

    terminate_pid(pid)
    return wait_for_pid_exit(pid, timeout)


def _primary_workspace_root(raw_root: object, default: Path) -> Path:
    return _workspace_roots(raw_root, default)[0]


def _workspace_roots(raw_root: object, default: Path) -> list[Path]:
    if isinstance(raw_root, list):
        values = raw_root or [default]
    else:
        values = [raw_root or default]
    roots: list[Path] = []
    for item in values:
        path = Path(str(item or default)).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return roots or [default.resolve()]
