"""Gateway process lifecycle, foreground run loop, status, stop/restart/logs commands.

Re-exports commands from _gateway_commands for backward compatibility.
"""

from __future__ import annotations

from ..agent.gateway import (
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    read_json_file,
    wait_for_gateway_running,
)
from ._gateway_commands import (
    cmd_gateway_install,
    cmd_gateway_logs,
    cmd_gateway_restart,
    cmd_gateway_run,
    cmd_gateway_start,
    cmd_gateway_status,
    cmd_gateway_stop,
    cmd_gateway_uninstall,
    get_running_pid,
    install_service,
    is_pid_alive,
    read_pid_record,
    read_runtime_status,
    remove_pid_file_if_owned,
    terminate_pid,
    uninstall_service,
    wait_for_pid_exit,
)
from ._gateway_process_service import (
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _gateway_request_worker_loop,
    _write_gateway_heartbeat,
)
from .common import make_agent

__all__ = [
    "cmd_gateway_install",
    "cmd_gateway_start",
    "cmd_gateway_status",
    "cmd_gateway_stop",
    "cmd_gateway_restart",
    "cmd_gateway_logs",
    "cmd_gateway_uninstall",
    "cmd_gateway_run",
    "gateway_running",
    "get_running_pid",
    "is_pid_alive",
    "gateway_paths",
    "make_agent",
    "read_json_file",
    "read_pid_record",
    "read_runtime_status",
    "remove_pid_file_if_owned",
    "terminate_pid",
    "wait_for_pid_exit",
    "wait_for_gateway_running",
    "gateway_request_counts",
    "install_service",
    "uninstall_service",
    "_gateway_heartbeat_loop",
    "_gateway_request_loop",
    "_gateway_request_worker_loop",
    "_write_gateway_heartbeat",
]