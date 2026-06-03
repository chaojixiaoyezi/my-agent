

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
    return _SERVICE_BASE


def _get_launchd_label() -> str:
    return "ai.my-agent.gateway"


def get_systemd_unit_path(system: bool = False) -> Path:
    name = get_service_name()
    if system:
        return Path("/etc/systemd/system") / f"{name}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def get_launchd_plist_path() -> Path:
    label = _get_launchd_label()
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


# =============================================================================
# Unit File Generation
# =============================================================================


def _detect_venv_dir() -> Path | None:
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


def _resolve_node_path() -> str | None:
    resolved_node = shutil.which("node")
    if resolved_node:
        return str(Path(resolved_node).resolve().parent)
    return None


def _build_systemd_path_entries(venv_bin: str) -> str:
    path_entries = [venv_bin]
    # Add node bin if present
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")
    if Path(node_bin).exists():
        path_entries.append(node_bin)
    # Add resolved node path
    resolved_node_dir = _resolve_node_path()
    if resolved_node_dir and resolved_node_dir not in path_entries:
        path_entries.append(resolved_node_dir)
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
    return ":".join(path_entries)


def _get_config_path() -> str:
    return os.environ.get("MY_AGENT_CONFIG", str(PROJECT_ROOT / "config" / "agent_config.yaml"))


def _format_systemd_unit_section(exec_start: str, working_dir: str, sane_path: str) -> str:
    return f"""[Service]
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


def generate_systemd_unit_text(system: bool = False) -> str:
    python_path = get_python_path()
    working_dir = str(PROJECT_ROOT)
    venv = _detect_venv_dir()
    venv_bin = str(venv / "bin") if venv else str(PROJECT_ROOT / "venv" / "bin")
    config_path = _get_config_path()
    sane_path = _build_systemd_path_entries(venv_bin)

    exec_start = f"{python_path} -m agent_py_agent --config {config_path} gateway run"

    service_section = _format_systemd_unit_section(exec_start, working_dir, sane_path)

    return f"""[Unit]
Description={_SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5
{service_section}
"""


# =============================================================================
# Launchd Plist Generation
# =============================================================================


def generate_launchd_plist_text() -> str:
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
