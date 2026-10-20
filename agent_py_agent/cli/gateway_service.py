# LLM: 服务与监护运输同一次原YAML/home，显式环境不继承错误根；渲染转义不授执行权，安装原副作用须用同一绑定。
# 模块用途: 生成和安装systemd/launchd服务，保持公开命令选定的配置路径。
from __future__ import annotations

"""Gateway host service generation and install helpers.

This module is the canonical CLI boundary for systemd/launchd service files.
It keeps unit generation and install/uninstall behavior in one current path.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

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


# LLM: 只消费原load_config/configured_home_root，不吞坏配置回落到另一home；renderer与installer运输同一次解析结果，不创建目录。
# 函数用途: 为服务冻结实际绝对配置和数据根，脱离交互环境后仍读写同一宿主目录。
def _service_runtime_paths(config_path=None):
    from ..agent.settings import load_config
    from ..agent.user_space.home_root import configured_home_root

    selected = Path(config_path if config_path is not None else _get_config_path()).expanduser().resolve()
    config = load_config(selected)
    home = Path(configured_home_root(config) or "~/.my-agent").expanduser().resolve()
    return selected, home


# LLM: systemd字段只接受无行分隔/NUL的真实路径，百分号保字面；ExecStart另保美元符号，不经shell解释或增加权限。
# 函数用途: 按systemd字符串规则保护服务参数及环境值，拒绝能拆开配置行的输入。
def _systemd_value(value, *, command=False):
    text = str(value)
    if any(char in text for char in "\r\n\0"):
        raise ValueError("服务路径包含不支持的换行或空字符，请改用普通绝对路径。")
    text = text.replace("%", "%%")
    if command:
        text = text.replace("$", "$$")
    return json.dumps(text, ensure_ascii=False)


# LLM: WorkingDirectory是原生单路径字段，不经argv/Environment的unquote；百分号仍须保字面，禁止行注入，普通空格/引号/反斜线原样保留。
# 函数用途: 编码systemd工作目录，避免外加JSON引号被当成路径字符。
def _systemd_directory_value(value) -> str:
    text = str(value)
    if any(char in text for char in "\r\n\0"):
        raise ValueError("服务路径包含不支持的换行或空字符，请改用普通绝对路径。")
    return text.replace("%", "%%")


# LLM: cwd只取同次已解析home，不能再按默认YAML重新选根；纯renderer不创建文件。
# 函数用途: 返回选定服务的中性工作目录，避免从源码目录遮蔽已安装包。
def _service_working_directory(config_path: str | Path | None = None, *, home_root: Path | None = None) -> Path:
    if home_root is None:
        _, home_root = _service_runtime_paths(config_path)
    return home_root / "service-cwd"


# LLM: 仅WorkingDirectory沿原生单路径字段，argv/Environment保各自原转义；不新增重启预算或内存策略。
# 函数用途: 生成systemd服务段，按字段自身语法运输原目录与环境值。
def _format_systemd_unit_section(exec_start: str, working_dir: str, sane_path: str, *, environment=None) -> str:
    variables = {"PATH": sane_path, **(environment or {})}
    lines = "\n".join("Environment=" + _systemd_value(key+"="+value) for key,value in variables.items())
    return f"""[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory={_systemd_directory_value(working_dir)}
{lines}
Restart=on-failure
RestartSec=30
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
"""


# LLM: Generated services must import the interpreter's installed package. Using PROJECT_ROOT as
# WorkingDirectory can put a stale checkout at sys.path[0] and silently defeat a successful wheel
# upgrade; keep the executable path and neutral cwd independent.
# LLM: 显式config_path只运输公开install已选文件，不把worker count复制成第二配置；仅渲染字符串。
# 函数用途: 生成绑定同一宿主配置的systemd单元，保留中性工作目录。
def generate_systemd_unit_text(system: bool = False, *, config_path: str | Path | None = None) -> str:
    selected, home = _service_runtime_paths(config_path)
    return _render_systemd_unit(system, selected, home)


# LLM: selected/home是原解析当次结果，renderer不重读环境或YAML；安装与纯生成共用，不能复制第二配置。
# 函数用途: 以同一配置和数据根生成完整systemd文本，不启动服务。
def _render_systemd_unit(system, config_path, home):
    del system
    python_path = get_python_path()
    working_dir = str(_service_working_directory(config_path, home_root=home))
    venv = _detect_venv_dir()
    venv_bin = str(venv / "bin") if venv else str(PROJECT_ROOT / "venv" / "bin")
    sane_path = _build_systemd_path_entries(venv_bin)
    command = [python_path, "-m", "agent_py_agent", "--config", str(config_path), "gateway", "run"]
    exec_start = " ".join(_systemd_value(value, command=True) for value in command)
    service_section = _format_systemd_unit_section(exec_start, working_dir, sane_path,
        environment={"MY_AGENT_CONFIG": str(config_path), "MY_AGENT_HOME": str(home)})
    return f"""[Unit]
Description={_SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=5
{service_section}
"""


# LLM: launchd follows the same neutral-cwd import contract as systemd; do not point it at a
# development checkout merely because the install command happened to run there.
# LLM: 显式config_path与公开start选择一致，仅渲染argv，不启动服务或持久保存拓扑数量。
# 函数用途: 生成绑定同一宿主配置的launchd文本，保持已安装包导入。
def generate_launchd_plist_text(*, config_path: str | Path | None = None) -> str:
    selected, home = _service_runtime_paths(config_path)
    return _render_launchd_plist(selected, home)


# LLM: plist只运输同一次原配置/home与原解释器，XML escape保每个值；不复制宿主其它环境或凭据。
# 函数用途: 生成明确配置、数据根及中性cwd的launchd文本，不创建目录或启动服务。
def _render_launchd_plist(config_path, home):
    python_path = get_python_path()
    label = _get_launchd_label()
    args = [python_path, "-m", "agent_py_agent", "--config", str(config_path), "gateway", "run"]
    program_args_xml = "".join(f"        <string>{escape(arg)}</string>\n" for arg in args)
    environment = {"MY_AGENT_CONFIG": str(config_path), "MY_AGENT_HOME": str(home)}
    environment_xml = "".join(f"        <key>{escape(key)}</key><string>{escape(value)}</string>\n"
                              for key,value in environment.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{escape(label)}</string>
    <key>ProgramArguments</key><array>
{program_args_xml}    </array>
    <key>EnvironmentVariables</key><dict>
{environment_xml}    </dict>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>RunAtLoad</key><true/>
    <key>WorkingDirectory</key><string>{escape(str(_service_working_directory(config_path, home_root=home)))}</string>
    <key>StandardOutPath</key><string>/tmp/{escape(label)}.stdout.log</string>
    <key>StandardErrorPath</key><string>/tmp/{escape(label)}.stderr.log</string>
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


# LLM: systemd refuses a missing WorkingDirectory before Python starts. Create exactly the
# canonical neutral directory before writing/enabling the unit; never create a source alias.
# 函数用途: 安装 systemd 服务，并预先建立不会遮蔽 wheel 的服务工作目录。
# LLM: 原配置/home只解析一次，先渲染校验再创建同根cwd/unit并调用systemctl；坏配置不回落或启动，原覆盖/拓扑语义不变。
# 函数用途: 安装绑定用户所选YAML的systemd服务，保留原覆盖确认规则。
def install_systemd(system: bool = False, force: bool = False, *, config_path: str | Path | None = None) -> bool:
    unit_path = get_systemd_unit_path(system=system)

    if unit_path.exists() and not force:
        print(f"Service already installed at: {unit_path}")
        print("Use --force to reinstall")
        return True

    scope = _service_scope_label(system)
    print(f"Installing {scope} systemd service to: {unit_path}")
    selected, home = _service_runtime_paths(config_path)
    text = _render_systemd_unit(system, selected, home)
    _service_working_directory(selected, home_root=home).mkdir(parents=True, exist_ok=True)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text, encoding="utf-8")

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


# LLM: launchd also requires the declared WorkingDirectory to exist. Keep directory creation at
# install time so pure plist generation stays read-only.
# 函数用途: 安装 launchd 服务，并建立统一的中性服务工作目录。
# LLM: 原配置/home只解析一次，先渲染校验再创建同根cwd/plist并调用launchctl；坏配置不回落，原覆盖/拓扑语义不变。
# 函数用途: 安装绑定用户所选YAML的launchd服务，保留原覆盖确认规则。
def install_launchd(force: bool = False, *, config_path: str | Path | None = None) -> bool:
    plist_path = get_launchd_plist_path()
    label = _get_launchd_label()

    if plist_path.exists() and not force:
        print(f"Service already installed at: {plist_path}")
        print("Use --force to reinstall")
        return True

    print(f"Installing launchd service to: {plist_path}")
    selected, home = _service_runtime_paths(config_path)
    text = _render_launchd_plist(selected, home)
    _service_working_directory(selected, home_root=home).mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(text, encoding="utf-8")

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


# LLM: 只沿当前平台原安装器写unit/plist并调用原服务管理器；config_path来自公开install同一解析入口。
# 函数用途: 安装引用同一宿主配置的系统服务，不生成第二份配置或worker flag。
def install_service(force: bool = False, *, config_path: str | Path | None = None) -> bool:
    if is_macos():
        return install_launchd(force=force, config_path=config_path)
    if is_linux() and supports_systemd_services():
        return install_systemd(system=False, force=force, config_path=config_path)
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
