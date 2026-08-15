# Gateway supervisor health reporting
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    WriteRuntimeStatusParams,
    get_running_pid,
    get_running_pid_report,
    read_pid_file,
    read_runtime_status,
    remove_pid_file,
    write_pid_file,
    write_runtime_status,
)
from .io import read_json_file_report
from .paths import gateway_paths
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit


@dataclass(frozen=True)
class SupervisorPayloadReport:
    payload: dict | None
    load_error: dict | None = None


def read_adapter_state(state_path: Path) -> dict:
    return read_adapter_state_report(state_path).payload or {}


def read_adapter_state_report(state_path: Path) -> SupervisorPayloadReport:
    if not state_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(state_path, context="gateway.supervisor.adapter_state.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


def read_gateway_heartbeat_report(heartbeat_path: Path) -> SupervisorPayloadReport:
    if not heartbeat_path.exists():
        return SupervisorPayloadReport(None)
    report = read_json_file_report(heartbeat_path, context="gateway.supervisor.heartbeat.read")
    return SupervisorPayloadReport(report.payload if report.payload else None, report.load_error)


def record_health_load_error(
    load_errors: list[dict],
    load_error: dict,
    *,
    log_warn: Callable[[str], None],
) -> None:
    load_errors.append(load_error)
    log_warn(
        "Gateway health load error: "
        f"context={load_error.get('context', '')} "
        f"path={load_error.get('path', '')} "
        f"error={load_error.get('message', '')}"
    )

def start_gateway(supervisor) -> int | None:
    supervisor._resolve_agent_and_paths()
    cmd = [sys.executable, "-m", "agent_py_agent", "--config", supervisor.config_path, "gateway", "run"]
    creationflags, start_new_session = _gateway_process_flags()
    supervisor._log_info(f"Starting gateway: {' '.join(cmd)}")
    try:
        process = _spawn_gateway_process(supervisor, cmd, creationflags, start_new_session)
    except OSError as exc:
        supervisor._log_error(f"Failed to spawn gateway process: {exc}")
        return None
    return _wait_for_gateway_start(supervisor, process)


def _gateway_process_flags() -> tuple[int, bool]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return flags, False
    return 0, True


def _spawn_gateway_process(supervisor, cmd: list[str], creationflags: int, start_new_session: bool):
    supervisor._paths.root.mkdir(parents=True, exist_ok=True)
    with supervisor._paths.log.open("ab") as log_file:
        return subprocess.Popen(
            cmd,
            cwd=str(Path(supervisor.config_path).resolve().parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )


def _wait_for_gateway_start(supervisor, process) -> int | None:
    deadline = time.time() + 120.0
    while time.time() < deadline:
        pid = get_running_pid(supervisor._paths.pid)
        if pid and is_pid_alive(pid):
            supervisor._log_info(f"Gateway started successfully: pid={pid}")
            return pid
        if not is_pid_alive(process.pid):
            supervisor._log_error(f"Gateway subprocess (pid={process.pid}) died during startup")
            return None
        time.sleep(0.5)
    supervisor._log_warn("Gateway started but PID not confirmed alive within 30s timeout")
    return process.pid


def stop_gateway(supervisor, timeout: float = 20.0) -> bool:
    supervisor._resolve_agent_and_paths()
    pid = get_running_pid(supervisor._paths.pid)
    if not pid:
        supervisor._log_info("Gateway already stopped")
        return True
    supervisor._log_info(f"Requesting gateway shutdown: pid={pid}")
    _write_supervisor_stop_request(supervisor)
    if wait_for_pid_exit(pid, timeout):
        supervisor._log_info("Gateway stopped gracefully")
        return True
    supervisor._log_warn(f"Gateway did not stop gracefully, force killing pid={pid}")
    terminate_pid(pid)
    if wait_for_pid_exit(pid, 5):
        supervisor._log_info("Gateway force-killed")
        return True
    supervisor._log_error("Failed to stop gateway even with force kill")
    return False


def _write_supervisor_stop_request(supervisor) -> None:
    supervisor._paths.stop_request.parent.mkdir(parents=True, exist_ok=True)
    supervisor._paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": "supervisor shutdown"}, ensure_ascii=False),
        encoding="utf-8",
    )


def restart_gateway(supervisor) -> bool:
    if supervisor._restart_count >= supervisor.max_restart_attempts:
        supervisor._log_error(
            f"Max restart attempts ({supervisor.max_restart_attempts}) reached. "
            "Supervisor will continue monitoring but not restart."
        )
        return False
    if time.time() - supervisor._last_restart_at < supervisor.restart_cooldown:
        supervisor._log_info(f"Restart cooldown active ({supervisor.restart_cooldown}s). Skipping.")
        return False
    supervisor._restart_count += 1
    supervisor._last_restart_at = time.time()
    supervisor._log_warn(
        f"Gateway unhealthy. Restarting (attempt {supervisor._restart_count}/{supervisor.max_restart_attempts})..."
    )
    pid = get_running_pid(supervisor._paths.pid)
    if pid and is_pid_alive(pid):
        supervisor._stop_gateway(timeout=10.0)
    gateway_pid = supervisor._start_gateway()
    if gateway_pid:
        supervisor._gateway_pid = gateway_pid
        return True
    supervisor._log_error("Failed to restart gateway")
    return False


def run_supervisor_loop(supervisor) -> int:
    supervisor._log_info("Gateway supervisor starting")
    supervisor._resolve_agent_and_paths()
    supervisor_pid_path = supervisor._paths.root / "supervisor.pid"
    _prepare_supervisor_run(supervisor, supervisor_pid_path)
    if not supervisor._is_gateway_healthy():
        supervisor._start_gateway()
    _log_supervisor_active(supervisor)
    _monitor_until_stopped(supervisor)
    _finish_supervisor_run(supervisor, supervisor_pid_path)
    return 0


def _prepare_supervisor_run(supervisor, supervisor_pid_path: Path) -> None:
    write_pid_file(supervisor_pid_path, os.getpid())
    signal.signal(signal.SIGTERM, supervisor._handle_signal)
    signal.signal(signal.SIGINT, supervisor._handle_signal)
    write_runtime_status(
        WriteRuntimeStatusParams(
            status_path=supervisor._paths.state,
            gateway_state="supervisor_running",
            restart_requested=False,
            active_agents=0,
        )
    )


def _log_supervisor_active(supervisor) -> None:
    supervisor._log_info(
        f"Supervisor active: check_interval={supervisor.check_interval}s, "
        f"heartbeat_timeout={supervisor.heartbeat_timeout}s, "
        f"max_restarts={supervisor.max_restart_attempts}"
    )


def _monitor_until_stopped(supervisor) -> None:
    last_check = time.time()
    consecutive_failures = 0
    while not supervisor._stop_requested:
        time.sleep(1.0)
        now = time.time()
        if now - last_check < supervisor.check_interval:
            continue
        last_check = now
        consecutive_failures = _run_health_check(supervisor, consecutive_failures)


def _run_health_check(supervisor, consecutive_failures: int) -> int:
    healthy = supervisor._is_gateway_healthy()
    adapter_healthy = supervisor._check_adapter_health()
    if healthy:
        return 0
    consecutive_failures += 1
    supervisor._log_warn(
        f"Gateway health check failed (consecutive={consecutive_failures}): "
        "PID file missing, process dead, or heartbeat stale"
    )
    if not adapter_healthy:
        supervisor._log_warn("Adapter is also not running")
    if consecutive_failures >= 3:
        supervisor._restart_gateway()
        return 0
    return consecutive_failures


def _finish_supervisor_run(supervisor, supervisor_pid_path: Path) -> None:
    supervisor._log_info("Supervisor shutting down...")
    supervisor._stop_gateway(timeout=15.0)
    try:
        supervisor_pid_path.unlink()
    except OSError:
        pass
    write_runtime_status(
        WriteRuntimeStatusParams(
            status_path=supervisor._paths.state,
            gateway_state="supervisor_stopped",
            restart_requested=False,
            active_agents=0,
        )
    )
    supervisor._log_info("Supervisor stopped")

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
        return start_gateway(self)

    def _stop_gateway(self, timeout: float = 20.0) -> bool:
        return stop_gateway(self, timeout=timeout)

    def _restart_gateway(self) -> bool:
        return restart_gateway(self)

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
