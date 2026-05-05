"""Unit file generation helpers for gateway service (systemd + launchd)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..agent.gateway_parts.daemon_control import GATEWAY_SERVICE_RESTART_EXIT_CODE

# Project root is agent_py_agent's parent directory
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Service name
_SERVICE_BASE = "my-agent-gateway"
_SERVICE_DESCRIPTION = "MyAgent Gateway - Multi-Agent Messaging Platform"


# =============================================================================
# Service Name & Paths
# =============================================================================


def get_service_name() -> str:
    """Return the systemd/launchd service name.

    Always returns the base name (no profile suffix for now).
    """
    return _SERVICE_BASE


def _get_launchd_label() -> str:
    """Return the launchd label (reverse-domain style)."""
    return "ai.my-agent.gateway"


def get_systemd_unit_path(system: bool = False) -> Path:
    """Return the path to the systemd unit file.

    Args:
        system: If True, return system service path (/etc/systemd/system/).
                If False, return user service path (~/.config/systemd/user/).
    """
    name = get_service_name()
    if system:
        return Path("/etc/systemd/system") / f"{name}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def get_launchd_plist_path() -> Path:
    """Return the path to the launchd plist file.

    Follows macOS convention: ~/Library/LaunchAgents/<label>.plist
    """
    label = _get_launchd_label()
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


# =============================================================================
# Unit File Generation
# =============================================================================


def _detect_venv_dir() -> Path | None:
    """Detect the active virtualenv directory.

    Checks sys.prefix first (works regardless of the directory name),
    then VIRTUAL_ENV env var, then falls back to probing common
    directory names under PROJECT_ROOT.
    Returns None when no virtualenv can be found.
    """
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv
    _virtual_env = os.environ.get("VIRTUAL_ENV")
    if _virtual_env:
        venv = Path(_virtual_env)
        if venv.is_dir():
            return venv
    for candidate in (".venv", "venv"):
        venv = PROJECT_ROOT / candidate
        if venv.is_dir():
            return venv
    return None


def get_python_path() -> str:
    """Return the Python interpreter path to use in the service."""
    venv = _detect_venv_dir()
    if venv is not None:
        if is_windows():
            venv_python = venv / "Scripts" / "python.exe"
        else:
            venv_python = venv / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
    return sys.executable


def _build_path_entries() -> list[str]:
    """Build PATH entries for the service environment."""
    path_entries = []
    venv = _detect_venv_dir()
    if venv:
        if is_windows():
            path_entries.append(str(venv / "Scripts"))
        else:
            path_entries.append(str(venv / "bin"))
    # Add node_modules bin if present
    node_bin = PROJECT_ROOT / "node_modules" / ".bin"
    if node_bin.exists():
        path_entries.append(str(node_bin))
    # Add common system bin paths
    common_bin_paths = [
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    path_entries.extend(common_bin_paths)
    return path_entries


def generate_systemd_unit_text(system: bool = False) -> str:
    """Generate systemd unit content for the gateway service.

    Args:
        system: If True, generate a system service (requires root).
                If False, generate a user service.
    """
    python_path = get_python_path()
    working_dir = str(PROJECT_ROOT)
    venv = _detect_venv_dir()
    venv_dir = str(venv) if venv else str(PROJECT_ROOT / "venv")
    venv_bin = str(venv / "bin") if venv else str(PROJECT_ROOT / "venv" / "bin")
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")

    path_entries = [venv_bin, node_bin]
    common_bin_paths = [
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]

    # Resolve node path
    resolved_node = shutil.which("node")
    if resolved_node:
        resolved_node_dir = str(Path(resolved_node).resolve().parent)
        if resolved_node_dir not in path_entries:
            path_entries.append(resolved_node_dir)

    path_entries.extend(common_bin_paths)
    sane_path = ":".join(path_entries)

    # Build the command - use config path from environment or default
    config_path = os.environ.get("MY_AGENT_CONFIG", str(PROJECT_ROOT / "config" / "agent_config.yaml"))

    if system:
        # System service - use absolute paths
        exec_start = f"{python_path} -m agent_py_agent --config {config_path} gateway run"
    else:
        # User service
        exec_start = f"{python_path} -m agent_py_agent --config {config_path} gateway run"

    return f"""[Unit]
Description={_SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={working_dir}
Environment="PATH={sane_path}"
Restart=on-failure
RestartSec=30
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
"""


# =============================================================================
# Launchd Plist Generation
# =============================================================================


def generate_launchd_plist_text() -> str:
    """Generate launchd plist XML content for the gateway service."""
    python_path = get_python_path()
    config_path = os.environ.get("MY_AGENT_CONFIG", str(PROJECT_ROOT / "config" / "agent_config.yaml"))
    label = _get_launchd_label()

    # Build ProgramArguments array
    args = [
        python_path,
        "-m",
        "agent_py_agent",
        "--config",
        config_path,
        "gateway",
        "run",
    ]
    program_args_xml = "".join(f"        <string>{arg}</string>\n" for arg in args)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args_xml}    </array>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>
    <key>StandardOutPath</key>
    <string>/tmp/{label}.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{label}.stderr.log</string>
</dict>
</plist>
"""


# =============================================================================
# Platform helpers used by generation functions
# =============================================================================


def is_windows() -> bool:
    return sys.platform == "win32"