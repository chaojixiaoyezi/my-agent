from __future__ import annotations

"""LLM: gateway watchdog supervisor — monitors gateway health and auto-restarts on crash.

给人看的解释：
这个文件实现一个独立的后台监督进程，负责监控 gateway 是否崩溃，并在崩溃后自动拉起。
不依赖 systemd，在 macOS/Linux 上都能跑。
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    acquire_scoped_lock,
    get_running_pid,
    read_pid_file,
    read_runtime_status,
    release_scoped_lock,
    remove_pid_file,
    write_pid_file,
    write_runtime_status,
)
from .io import read_json_file
from .paths import gateway_paths
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GatewaySupervisor:
    """Watchdog that monitors a gateway process and auto-restarts on crash."""

    def __init__(
        self,
        config_path: str,
        *,
        workspace_root: Optional[str] = None,
        heartbeat_timeout: float = 120.0,
        check_interval: float = 10.0,
        max_restart_attempts: int = 5,
        restart_cooldown: float = 30.0,
        log_path: Optional[Path] = None,
    ):
        self.config_path = config_path
        self.workspace_root = workspace_root
        self.heartbeat_timeout = heartbeat_timeout
        self.check_interval = check_interval
        self.max_restart_attempts = max_restart_attempts
        self.restart_cooldown = restart_cooldown
        self.log_path = log_path

        self._supervisor_pid: int = os.getpid()
        self._gateway_pid: Optional[int] = None
        self._restart_count: int = 0
        self._last_restart_at: float = 0.0
        self._stop_requested: bool = False
        self._agent = None
        self._paths = None

    def _resolve_agent_and_paths(self):
        """Lazily resolve agent and paths to avoid import overhead in supervisor."""
        if self._agent is not None:
            return

        # Import lazily to avoid circular dependencies
        from ..core import SimpleAgent
        from ..config import load_config

        config = load_config(self.config_path)
        root = Path(self.config_path).resolve().parent
        if self.workspace_root:
            root = Path(self.workspace_root).resolve()

        self._agent = SimpleAgent(config, root)
        self._paths = gateway_paths(self._agent)

    def _log(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {msg}"
        print(line, flush=True)
        if self.log_path:
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

    def _read_gateway_heartbeat(self) -> Optional[dict]:
        """Read gateway heartbeat file, returning None if missing or stale."""
        self._resolve_agent_and_paths()
        heartbeat_path = self._paths.heartbeat
        if not heartbeat_path.exists():
            return None
        try:
            return json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _is_gateway_healthy(self) -> bool:
        """Check if the gateway is healthy (running and heartbeat fresh)."""
        self._resolve_agent_and_paths()

        # Check heartbeat freshness first (most reliable)
        heartbeat = self._read_gateway_heartbeat()
        if heartbeat:
            updated_at = heartbeat.get("updated_at", 0)
            if updated_at:
                age = time.time() - float(updated_at)
                if age <= self.heartbeat_timeout:
                    return True  # Gateway is alive and responsive
                else:
                    self._log_warn(f"Gateway heartbeat stale: age={age:.1f}s > {self.heartbeat_timeout}s")

        # Fallback: check PID file directly
        pid = get_running_pid(self._paths.pid)
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        # PID file is fresh but no heartbeat - could be startup phase
        if not heartbeat:
            self._log_info("Gateway PID alive but no heartbeat yet (startup phase)")
            return True

        return False

    def _check_adapter_health(self) -> bool:
        """Check if the adapter is healthy (running and state file valid)."""
        self._resolve_agent_and_paths()

        # Check adapter PID file
        pid = get_running_pid(self._paths.adapter_pid)
        if not pid:
            return False

        if not is_pid_alive(pid):
            return False

        # Check adapter state file
        state_path = self._paths.root / "adapter_state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                # Check if state is "running"
                if state.get("state") == "running":
                    return True
            except (OSError, json.JSONDecodeError):
                pass

        return True  # PID alive, assume healthy if no state file

    def _start_gateway(self) -> Optional[int]:
        """Start the gateway process. Returns the gateway PID or None on failure."""
        self._resolve_agent_and_paths()

        cmd = [
            sys.executable,
            "-m",
            "agent_py_agent",
            "--config",
            self.config_path,
            "gateway",
            "run",
        ]

        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            start_new_session = True

        self._log_info(f"Starting gateway: {' '.join(cmd)}")

        try:
            self._paths.root.mkdir(parents=True, exist_ok=True)
            with self._paths.log.open("ab") as log_file:
                process = subprocess.Popen(
                    cmd,
                    cwd=str(Path(self.config_path).resolve().parent.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    start_new_session=start_new_session,
                )
        except OSError as exc:
            self._log_error(f"Failed to spawn gateway process: {exc}")
            return None

        # Wait for the gateway to write its PID file and become alive (allow up to 120s)
        deadline = time.time() + 120.0
        while time.time() < deadline:
            pid = get_running_pid(self._paths.pid)
            if pid and is_pid_alive(pid):
                self._log_info(f"Gateway started successfully: pid={pid}")
                return pid
            # Also check if the subprocess itself is still alive (before PID file is written)
            if not is_pid_alive(process.pid):
                self._log_error(f"Gateway subprocess (pid={process.pid}) died during startup")
                return None
            time.sleep(0.5)

        self._log_warn("Gateway started but PID not confirmed alive within 30s timeout")
        # Return the subprocess PID anyway (gateway might still be initializing)
        return process.pid

    def _stop_gateway(self, timeout: float = 20.0) -> bool:
        """Request graceful shutdown of the gateway. Returns True if stopped."""
        self._resolve_agent_and_paths()

        pid = get_running_pid(self._paths.pid)
        if not pid:
            self._log_info("Gateway already stopped")
            return True

        self._log_info(f"Requesting gateway shutdown: pid={pid}")

        # Write stop request
        self._paths.stop_request.parent.mkdir(parents=True, exist_ok=True)
        self._paths.stop_request.write_text(
            json.dumps({"requested_at": time.time(), "reason": "supervisor shutdown"}, ensure_ascii=False),
            encoding="utf-8",
        )

        if wait_for_pid_exit(pid, timeout):
            self._log_info("Gateway stopped gracefully")
            return True

        # Force kill
        self._log_warn(f"Gateway did not stop gracefully, force killing pid={pid}")
        terminate_pid(pid)
        if wait_for_pid_exit(pid, 5):
            self._log_info("Gateway force-killed")
            return True

        self._log_error("Failed to stop gateway even with force kill")
        return False

    def _restart_gateway(self) -> bool:
        """Restart the gateway. Returns True if restart was attempted."""
        if self._restart_count >= self.max_restart_attempts:
            self._log_error(
                f"Max restart attempts ({self.max_restart_attempts}) reached. "
                "Supervisor will continue monitoring but not restart."
            )
            return False

        # Check cooldown
        if time.time() - self._last_restart_at < self.restart_cooldown:
            self._log_info(f"Restart cooldown active ({self.restart_cooldown}s). Skipping.")
            return False

        self._restart_count += 1
        self._last_restart_at = time.time()

        self._log_warn(
            f"Gateway unhealthy. Restarting (attempt {self._restart_count}/{self.max_restart_attempts})..."
        )

        pid = get_running_pid(self._paths.pid)
        if pid and is_pid_alive(pid):
            self._stop_gateway(timeout=10.0)

        gateway_pid = self._start_gateway()
        if gateway_pid:
            self._gateway_pid = gateway_pid
            return True
        else:
            self._log_error("Failed to restart gateway")
            return False

    def _handle_signal(self, signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._log_info(f"Received {sig_name}, initiating graceful shutdown...")
        self._stop_requested = True

    def run(self) -> int:
        """Run the supervisor loop. Blocks until stop is requested."""
        self._log_info("Gateway supervisor starting")
        self._resolve_agent_and_paths()

        # Write supervisor PID file
        supervisor_pid_path = self._paths.root / "supervisor.pid"
        write_pid_file(supervisor_pid_path, os.getpid())

        # Install signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        # Write initial status
        write_runtime_status(
            self._paths.state,
            gateway_state="supervisor_running",
            restart_requested=False,
            active_agents=0,
        )

        # Start gateway if not already running
        if not self._is_gateway_healthy():
            self._start_gateway()

        self._log_info(
            f"Supervisor active: check_interval={self.check_interval}s, "
            f"heartbeat_timeout={self.heartbeat_timeout}s, "
            f"max_restarts={self.max_restart_attempts}"
        )

        last_check = time.time()
        consecutive_failures = 0

        while not self._stop_requested:
            time.sleep(1.0)

            # Check health at configured interval
            now = time.time()
            if now - last_check < self.check_interval:
                continue
            last_check = now

            healthy = self._is_gateway_healthy()
            adapter_healthy = self._check_adapter_health()

            if healthy:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                self._log_warn(
                    f"Gateway health check failed (consecutive={consecutive_failures}): "
                    "PID file missing, process dead, or heartbeat stale"
                )
                if not adapter_healthy:
                    self._log_warn("Adapter is also not running")

            if consecutive_failures >= 3:
                self._restart_gateway()
                consecutive_failures = 0

        # Graceful shutdown
        self._log_info("Supervisor shutting down...")
        self._stop_gateway(timeout=15.0)

        # Cleanup
        try:
            supervisor_pid_path.unlink()
        except OSError:
            pass

        write_runtime_status(
            self._paths.state,
            gateway_state="supervisor_stopped",
            restart_requested=False,
            active_agents=0,
        )

        self._log_info("Supervisor stopped")
        return 0


def run_supervisor(config_path: str, **kwargs) -> int:
    """Run the gateway supervisor with the given config."""
    supervisor = GatewaySupervisor(config_path, **kwargs)
    return supervisor.run()


def is_supervisor_running(config_path: str) -> bool:
    """Check if a supervisor is running for the given config."""
    from ..core import SimpleAgent
    from ..config import load_config

    config = load_config(config_path)
    root = Path(config_path).resolve().parent
    agent = SimpleAgent(config, root)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    if not supervisor_pid_path.exists():
        return False

    pid = read_pid_file(supervisor_pid_path)
    if not pid:
        return False

    return is_pid_alive(pid)


def stop_supervisor(config_path: str, timeout: float = 10.0) -> bool:
    """Stop the supervisor for the given config."""
    from ..core import SimpleAgent
    from ..config import load_config

    config = load_config(config_path)
    root = Path(config_path).resolve().parent
    agent = SimpleAgent(config, root)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    pid = read_pid_file(supervisor_pid_path)
    if not pid or not is_pid_alive(pid):
        return True

    terminate_pid(pid)
    return wait_for_pid_exit(pid, timeout)
