from __future__ import annotations

"""Gateway host service generation and install helpers.

This module is the canonical CLI boundary for systemd/launchd service files.
It keeps unit generation and install/uninstall behavior in one current path.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..agent.gateway_parts.daemon_control import GATEWAY_SERVICE_RESTART_EXIT_CODE
from .bootstrap import default_config_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SERVICE_BASE = "my-agent-gateway"
_SERVICE_DESCRIPTION = "MyAgent Gateway - Multi-Agent Messaging Platform"


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


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def supports_systemd_services() -> bool:
    if not is_linux():
        return False
    if shutil.which("systemctl") is None:
        return False
    try:
        result = subprocess.run(
            ["systemctl", "list-timers", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode in (0, 1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_venv_dir() -> Path | None:
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        venv = Path(virtual_env)
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
        venv_python = venv / "Scripts" / "python.exe" if is_windows() else venv / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
    return sys.executable


def _resolve_node_path() -> str | None:
    resolved_node = shutil.which("node")
    if resolved_node:
        return str(Path(resolved_node).resolve().parent)
    return None


def _build_systemd_path_entries(venv_bin: str) -> str:
    path_entries = [venv_bin]
    node_bin = str(PROJECT_ROOT / "node_modules" / ".bin")
    if Path(node_bin).exists():
        path_entries.append(node_bin)
    resolved_node_dir = _resolve_node_path()
    if resolved_node_dir and resolved_node_dir not in path_entries:
        path_entries.append(resolved_node_dir)
    path_entries.extend([
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ])
    return ":".join(path_entries)


# LLM: Service files and interactive parsers must use the same default-config resolver; do not
# duplicate environment fallback logic here or deployments can split into two model configs.
# 函数用途: 生成服务启动命令时取得默认配置路径，与裸 my-agent 的选择保持一致。
def _get_config_path() -> str:
    return str(default_config_path())


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
    del system
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


def generate_launchd_plist_text() -> str:
    python_path = get_python_path()
    config_path = _get_config_path()
    label = _get_launchd_label()
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


def _ensure_user_systemd_env() -> None:
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
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


def _run_systemctl(
    args: list[str], system: bool = False, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess:
    cmd = _systemctl_cmd(system) + args
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout)


def _service_scope_label(system: bool) -> str:
    return "system" if system else "user"


def install_systemd(system: bool = False, force: bool = False) -> bool:
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
    except subprocess.CalledProcessError as exc:
        print(f"Failed to enable service: {exc.stderr}")
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
    try:
        _run_systemctl(["stop", get_service_name()], system=system, check=False, timeout=90)
        _run_systemctl(["disable", get_service_name()], system=system, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        print("Warning: systemctl command timed out during stop/disable")

    unit_path = get_systemd_unit_path(system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f"✓ Removed {unit_path}")

    try:
        _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Warning: daemon-reload failed")

    print(f"✓ {_service_scope_label(system).capitalize()} service uninstalled")
    return True


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def install_launchd(force: bool = False) -> bool:
    plist_path = get_launchd_plist_path()
    label = _get_launchd_label()

    if plist_path.exists() and not force:
        print(f"Service already installed at: {plist_path}")
        print("Use --force to reinstall")
        return True

    print(f"Installing launchd service to: {plist_path}")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(generate_launchd_plist_text(), encoding="utf-8")

    try:
        subprocess.run(
            ["launchctl", "bootstrap", _launchd_domain(), str(plist_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Failed to bootstrap launchd service: {exc.stderr}")
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
    plist_path = get_launchd_plist_path()
    label = _get_launchd_label()

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

    if plist_path.exists():
        plist_path.unlink()
        print(f"✓ Removed {plist_path}")

    print("✓ Service uninstalled")
    return True


def install_service(force: bool = False) -> bool:
    if is_macos():
        return install_launchd(force=force)
    if is_linux() and supports_systemd_services():
        return install_systemd(system=False, force=force)
    print("Service installation is only supported on Linux (systemd) and macOS (launchd)")
    return False


def uninstall_service() -> bool:
    if is_macos():
        return uninstall_launchd()
    if is_linux() and supports_systemd_services():
        return uninstall_systemd(system=False)
    print("Service uninstallation is only supported on Linux (systemd) and macOS (launchd)")
    return False


__all__ = [
    "PROJECT_ROOT",
    "generate_launchd_plist_text",
    "generate_systemd_unit_text",
    "get_launchd_plist_path",
    "get_service_name",
    "get_systemd_unit_path",
    "install_service",
    "install_systemd",
    "is_linux",
    "is_macos",
    "is_windows",
    "supports_systemd_services",
    "uninstall_service",
    "uninstall_systemd",
]
