# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。


from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ._gateway_service_unit_gen import (
    PROJECT_ROOT,
    _get_launchd_label,
    generate_launchd_plist_text,
    generate_systemd_unit_text,
    get_launchd_plist_path,
    get_service_name,
    get_systemd_unit_path,
)

# =============================================================================
# Platform Detection
# =============================================================================


# LLM: is_macos 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def is_macos() -> bool:
    return sys.platform == "darwin"


# LLM: is_linux 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 判断输入或环境是否满足规则，结果会影响分支、告警或阻断。
def is_linux() -> bool:
    return sys.platform.startswith("linux")


# LLM: supports_systemd_services 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def supports_systemd_services() -> bool:
    import shutil

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
# Systemd Install/Uninstall
# =============================================================================


# LLM: _ensure_user_systemd_env 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: _systemctl_cmd 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _systemctl_cmd(system: bool = False) -> list[str]:
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


# LLM: _run_systemctl 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def _run_systemctl(
    args: list[str], system: bool = False, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess:
    cmd = _systemctl_cmd(system) + args
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout)


# LLM: _service_scope_label 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _service_scope_label(system: bool) -> str:
    return "system" if system else "user"


# LLM: install_systemd 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: uninstall_systemd 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def uninstall_systemd(system: bool = False) -> bool:
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


# LLM: _launchd_domain 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


# LLM: install_launchd 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
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


# LLM: uninstall_launchd 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def uninstall_launchd() -> bool:
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


# LLM: install_service 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def install_service(force: bool = False) -> bool:
    if is_macos():
        return install_launchd(force=force)
    elif is_linux() and supports_systemd_services():
        return install_systemd(system=False, force=force)
    else:
        print("Service installation is only supported on Linux (systemd) and macOS (launchd)")
        return False


# LLM: uninstall_service 属于gateway CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def uninstall_service() -> bool:
    if is_macos():
        return uninstall_launchd()
    elif is_linux() and supports_systemd_services():
        return uninstall_systemd(system=False)
    else:
        print("Service uninstallation is only supported on Linux (systemd) and macOS (launchd)")
        return False
