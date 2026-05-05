from __future__ import annotations

"""LLM: gateway watchdog supervisor — monitors gateway health and auto-restarts on crash.

给人看的解释：
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
from typing import Optional

from .daemon_control import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    WriteRuntimeStatusParams,
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


@dataclass
class SupervisorConfig:
    """Bundle for GatewaySupervisor optional configuration parameters."""

    workspace_root: str | None = None
    heartbeat_timeout: float = 120.0
    check_interval: float = 10.0
    max_restart_attempts: int = 5
    restart_cooldown: float = 30.0
    log_path: Path | None = None


class GatewaySupervisor:
    """Watchdog that monitors a gateway process and auto-restarts on crash."""

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

    def _resolve_agent_and_paths(self):
        """Lazily resolve agent and paths to avoid import overhead in supervisor."""
        if self._agent is not None or self._paths is not None:
            return

        # Import lazily to avoid circular dependencies
        from ..config import load_config
        from ..core import SimpleAgent

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

    def _read_gateway_heartbeat(self) -> dict | None:
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
                    self._log_warn(
                        f"Gateway heartbeat stale: age={age:.1f}s > {self.heartbeat_timeout}s"
                    )

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

    def _start_gateway(self) -> int | None:
        """Start the gateway process. Returns the gateway PID or None on failure."""
        return _start_gateway_impl(self)

    def _stop_gateway(self, timeout: float = 20.0) -> bool:
        """Request graceful shutdown of the gateway. Returns True if stopped."""
        return _stop_gateway_impl(self, timeout=timeout)

    def _restart_gateway(self) -> bool:
        """Restart the gateway. Returns True if restart was attempted."""
        return _restart_gateway_impl(self)

    def _handle_signal(self, signum, frame) -> None:
        sig_name = signal.Signals(signum).name
        self._log_info(f"Received {sig_name}, initiating graceful shutdown...")
        self._stop_requested = True

    def run(self) -> int:
        """Run the supervisor loop. Blocks until stop is requested."""
        return run_supervisor_loop(self)


def run_supervisor(config_path: str, **kwargs) -> int:
    """Run the gateway supervisor with the given config."""
    options = SupervisorConfig(**kwargs) if kwargs else None
    supervisor = GatewaySupervisor(config_path, options=options)
    return supervisor.run()


def is_supervisor_running(config_path: str) -> bool:
    """Check if a supervisor is running for the given config."""
    from ..config import load_config
    from ..core import SimpleAgent

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
    from ..config import load_config
    from ..core import SimpleAgent

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
