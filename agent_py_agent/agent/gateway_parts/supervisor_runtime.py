
from __future__ import annotations

"""Runtime operations used by GatewaySupervisor."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .daemon_control import (
    WriteRuntimeStatusParams,
    get_running_pid,
    write_pid_file,
    write_runtime_status,
)
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit


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
