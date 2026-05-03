from __future__ import annotations

"""LLM: gateway service installation/uninstallation for systemd (Linux) and launchd (macOS).

Following Hermes's pattern:
- systemd uses Restart=on-failure, RestartSec=30, RestartForceExitStatus=75
- launchd uses KeepAlive with SuccessfulExit=false
- User services live in ~/.config/systemd/user/ (non-root)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..agent.gateway_parts.daemon_control import GATEWAY_SERVICE_RESTART_EXIT_CODE

# Service name
_SERVICE_BASE = "my-agent-gateway"
_SERVICE_DESCRIPTION = "MyAgent Gateway - Multi-Agent Messaging Platform"


# =============================================================================
# Platform Detection
# =============================================================================


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_windows() -> bool:
    return sys.platform == "win32"


def supports_systemd_services() -> bool:
    """Return True when systemd user services are available."""
    if not is_linux():
        return False
    if shutil.which("systemctl") is None:
        return False
    # Check if systemctl can talk to systemd (works in containers too)
    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode in (0, 1)  # 1 is okay (empty list)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


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
# Systemd Install/Uninstall
# =============================================================================


def _ensure_user_systemd_env() -> None:
    """Ensure DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR are set for systemctl --user.

    On headless servers (SSH sessions), these env vars may be missing even when
    the user's systemd instance is running (via linger). Without them,
    ``systemctl --user`` fails with "Failed to connect to bus: No medium found".
    """
    uid = os.getuid()
    if "XDG_RUNTIME_DIR" not in os.environ:
        runtime_dir = f"/run/user/{uid}"
        if Path(runtime_dir).exists():
            os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        bus_path = Path(xdg_runtime) / "bus"
        if bus_path.exists():
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def _systemctl_cmd(system: bool = False) -> list[str]:
    """Return the systemctl command prefix for user or system service."""
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


def _run_systemctl(args: list[str], system: bool = False, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run systemctl with the given arguments."""
    cmd = _systemctl_cmd(system) + args
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout)


def _service_scope_label(system: bool) -> str:
    return "system" if system else "user"


def install_systemd(system: bool = False, force: bool = False) -> bool:
    """Install the systemd service unit file and enable it.

    Args:
        system: If True, install as system service (requires root).
                If False, install as user service.
        force: If True, reinstall even if service already exists.

    Returns:
        True if installation was successful or already installed (not force).
        False if installation failed.
    """
    unit_path = get_systemd_unit_path(system=system)

    if unit_path.exists() and not force:
        print(f"Service already installed at: {unit_path}")
        print("Use --force to reinstall")
        return True

    scope = _service_scope_label(system)
    print(f"Installing {scope} systemd service to: {unit_path}")

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(generate_systemd_unit_text(system=system), encoding="utf-8")

    try:
        _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
        _run_systemctl(["enable", get_service_name()], system=system, check=True, timeout=30)
    except subprocess.CalledProcessError as e:
        print(f"Failed to enable service: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("systemctl command timed out")
        return False

    print()
    print(f"✓ {scope.capitalize()} service installed and enabled!")
    print()
    print("Next steps:")
    if system:
        print(f"  sudo systemctl start {get_service_name()}           # Start the service")
        print(f"  sudo systemctl status {get_service_name()}          # Check status")
        print(f"  journalctl -u {get_service_name()} -f              # View logs")
    else:
        print(f"  systemctl --user start {get_service_name()}        # Start the service")
        print(f"  systemctl --user status {get_service_name()}       # Check status")
        print(f"  journalctl --user -u {get_service_name()} -f       # View logs")
        print()
        print("Note: For user services to survive logout, enable linger:")
        print("  sudo loginctl enable-linger $USER")

    return True


def uninstall_systemd(system: bool = False) -> bool:
    """Stop, disable, and remove the systemd service unit file.

    Args:
        system: If True, uninstall system service (requires root).
                If False, uninstall user service.

    Returns:
        True if uninstallation was successful.
        False if uninstallation failed.
    """
    scope = _service_scope_label(system)

    # Stop and disable the service (ignore errors if not running)
    try:
        _run_systemctl(["stop", get_service_name()], system=system, check=False, timeout=90)
        _run_systemctl(["disable", get_service_name()], system=system, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        print("Warning: systemctl command timed out during stop/disable")

    # Remove the unit file
    unit_path = get_systemd_unit_path(system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f"✓ Removed {unit_path}")

    # Reload daemon
    try:
        _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Warning: daemon-reload failed")

    print(f"✓ {_service_scope_label(system).capitalize()} service uninstalled")
    return True


# =============================================================================
# Launchd Install/Uninstall
# =============================================================================

def _launchd_domain() -> str:
    """Return the launchd domain for the current user."""
    return f"gui/{os.getuid()}"


def install_launchd(force: bool = False) -> bool:
    """Install the launchd plist and load it.

    Args:
        force: If True, reinstall even if service already exists.

    Returns:
        True if installation was successful.
        False if installation failed.
    """
    plist_path = get_launchd_plist_path()
    label = _get_launchd_label()

    if plist_path.exists() and not force:
        print(f"Service already installed at: {plist_path}")
        print("Use --force to reinstall")
        return True

    print(f"Installing launchd service to: {plist_path}")

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(generate_launchd_plist_text(), encoding="utf-8")

    # Bootstrap the service (load into launchd)
    try:
        subprocess.run(
            ["launchctl", "bootstrap", _launchd_domain(), str(plist_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        print(f"Failed to bootstrap launchd service: {e.stderr}")
        return False
    except subprocess.TimeoutExpired:
        print("launchctl bootstrap timed out")
        return False

    print()
    print("✓ Service installed and loaded!")
    print()
    print("Next steps:")
    print(f"  launchctl bootout gui/$(id -u)/{label}     # Unload the service")
    print(f"  launchctl print gui/$(id -u)/{label}      # Check status")
    print(f"  tail -f /tmp/{label}.stdout.log          # View stdout logs")

    return True


def uninstall_launchd() -> bool:
    """Unload and remove the launchd plist.

    Returns:
        True if uninstallation was successful.
        False if uninstallation failed.
    """
    plist_path = get_launchd_plist_path()
    label = _get_launchd_label()

    # Bootout the service (unload from launchd)
    try:
        subprocess.run(
            ["launchctl", "bootout", f"{_launchd_domain()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        print("Warning: launchctl bootout timed out")

    # Remove the plist file
    if plist_path.exists():
        plist_path.unlink()
        print(f"✓ Removed {plist_path}")

    print("✓ Service uninstalled")
    return True


# =============================================================================
# Convenience Functions
# =============================================================================


def install_service(force: bool = False) -> bool:
    """Install the gateway service for the current platform.

    On Linux with systemd: installs a systemd user service.
    On macOS: installs a launchd service.
    On other platforms: returns False (not supported).
    """
    if is_macos():
        return install_launchd(force=force)
    elif is_linux() and supports_systemd_services():
        return install_systemd(system=False, force=force)
    else:
        print("Service installation is only supported on Linux (systemd) and macOS (launchd)")
        return False


def uninstall_service() -> bool:
    """Uninstall the gateway service for the current platform.

    On Linux with systemd: uninstalls the systemd user service.
    On macOS: uninstalls the launchd service.
    On other platforms: returns False (not supported).
    """
    if is_macos():
        return uninstall_launchd()
    elif is_linux() and supports_systemd_services():
        return uninstall_systemd(system=False)
    else:
        print("Service uninstallation is only supported on Linux (systemd) and macOS (launchd)")
        return False


# =============================================================================
# Module-level constants (must be at bottom after all function defs)
# =============================================================================

# Project root is agent_py_agent's parent directory
PROJECT_ROOT = Path(__file__).resolve().parents[2]
