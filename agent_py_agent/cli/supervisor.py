# LLM: CLI只读独立supervisor身份，Gateway ready复用同角色健康读门并运输make_agent原Agent/配置；不得补默认N或另建Agent。
# 模块用途: 唯一装配监护用实际Agent及规范路径，首启沿私有no-follow口准备设施；角色状态分离，不传构造callback。

from __future__ import annotations

"""implements gateway supervisor and start-all CLI commands.

给人看的解释：
这个文件实现两个功能：
1. supervisor 子命令：启动/停止/查看 gateway 看门狗进程
2. start-all 子命令：一键启动 gateway + 所有通道适配器
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..agent.common.json_io import write_private_text_file_atomic
from ..agent.common.nofollow_fs import ensure_private_dir
from ..agent.gateway_parts import gateway_paths, gateway_running
from ..agent.gateway_parts.daemon_control import get_running_pid, get_running_pid_report
from ..agent.gateway_parts.io import write_json_file_atomic
from ..agent.gateway_parts.paths import GatewayPaths
from ..agent.gateway_parts.supervisor import (
    GatewaySupervisor,
    clear_maintenance_lock,
    is_supervisor_running,
    read_supervisor_process_report,
    run_supervisor,
    stop_supervisor,
    write_maintenance_lock,
)
from .common import ROOT, make_agent


# LLM: 私有安装须同一祖先同时有标记和稳定启动器；不引用版本化runtime解释器，源码环境保留当前Python。
# 函数用途: 为OS看门狗选择升级后仍稳定的启动命令。
def resolve_stable_supervisor_command(config_path: str | Path, home_root: Path | None = None) -> list[str]:
    for source in (config_path, home_root):
        if source is None:
            continue
        candidate = Path(source).expanduser().absolute()
        for prefix in (candidate, *candidate.parents):
            launcher = prefix / "bin" / "my-agent"
            if (prefix / ".my-agent-private-install.json").is_file() and launcher.is_file():
                return [str(launcher), "--config", str(config_path), "gateway", "supervisor"]
    return [sys.executable, "-m", "agent_py_agent", "--config", str(config_path), "gateway", "supervisor"]


# LLM: unit是systemctl的位置参数且只能在本人user目录内；拒绝路径、控制字符和前导选项。
# 函数用途: 验证服务名并派生唯一unit文件地址。
def _supervisor_unit_path(unit_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*\.service", unit_name):
        raise ValueError("unit必须是不含路径的.service名称")
    return Path.home() / ".config" / "systemd" / "user" / unit_name


# LLM: ExecStart用冒号禁用变量展开；仅参数去引号/反斜线，WorkingDirectory按原样解析，只转义specifier。
# 函数用途: 按systemd字段语法编码字面参数或工作目录。
def _systemd_literal(value: str | Path, *, quoted: bool = True) -> str:
    text = str(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError("服务路径不能含控制字符")
    text = text.replace("%", "%%")
    if not quoted:
        if text != text.strip() or text.endswith("\\"):
            raise ValueError("工作目录不能含边缘空白或末尾反斜线")
        return text
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


# LLM: 安装只描述supervisor；工作目录不套引号，命令用冒号禁用环境展开保留美元字面量，启动状态另由回执给出。
# 函数用途: 构造用户级OS看门狗unit文本。
def _supervisor_unit_text(command: list[str], config_path: str | Path, home_root: Path) -> str:
    executable = " ".join(_systemd_literal(part) for part in command)
    return ("[Unit]\nDescription=My-Agent Gateway Supervisor\nAfter=network.target\n\n[Service]\nType=simple\n"
            f"WorkingDirectory={_systemd_literal(home_root, quoted=False)}\n"
            f"Environment={_systemd_literal('MY_AGENT_HOME=' + str(home_root))}\n"
            f"Environment={_systemd_literal('MY_AGENT_CONFIG=' + str(config_path))}\n"
            f"ExecStart=:{executable}\nRestart=always\nRestartSec=15s\n\n[Install]\nWantedBy=default.target\n")


# LLM: 仅Linux且systemctl存在时触及本人user bus；命令有界，异常和非零返回都保留错误，不伪称启动。
# 函数用途: 执行一组用户服务动作并返回是否执行及错误列表。
def _run_user_systemctl(actions: list[list[str]]) -> tuple[bool, list[str]]:
    executable = shutil.which("systemctl") if sys.platform.startswith("linux") else None
    if not executable:
        return False, []
    errors = []
    for action in actions:
        try:
            result = subprocess.run([executable, "--user", *action], capture_output=True, text=True, timeout=15)
            if result.returncode:
                errors.append(f"{' '.join(action)}: {result.stderr or result.stdout or result.returncode}")
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"{' '.join(action)}: {error}")
    return True, errors


# LLM: 只写本人user unit与规范回执，启动可禁用；失败记录返回调用方，不修改私有安装器或已有Gateway服务。
# 函数用途: 原子安装稳定命令的OS看门狗，并按需启用。
def install_supervisor_systemd_service(paths: GatewayPaths, *, config_path: str | Path, home_root: Path,
                                       unit_name: str = "my-agent-gw-supervisor.service", start_now: bool = True) -> dict:
    unit_path = _supervisor_unit_path(unit_name)
    config_path = Path(config_path).expanduser().absolute()
    home_root = Path(home_root).expanduser().absolute()
    cmd = resolve_stable_supervisor_command(config_path, home_root)
    text = _supervisor_unit_text(cmd, config_path, home_root)
    write_private_text_file_atomic(unit_path, text)
    record = {"schema": "my-agent.supervisor-service.v1", "unit_name": unit_name,
              "unit_path": str(unit_path), "command": cmd, "updated_at": time.time()}
    write_json_file_atomic(paths.supervisor_service, record)
    attempted, errors = _run_user_systemctl([["daemon-reload"], ["enable", "--now", unit_name]]) if start_now else (False, [])
    return {**record, "started": attempted and not errors, "errors": errors}


# LLM: unit名同安装门校验；先尝试disable --now，删除本人unit后reload，仅删除匹配的安装回执。
# 函数用途: 卸载指定OS看门狗并保留无法确认停止的错误。
def uninstall_supervisor_systemd_service(paths: GatewayPaths, *, unit_name: str = "my-agent-gw-supervisor.service") -> dict:
    unit_path = _supervisor_unit_path(unit_name)
    attempted, errors = _run_user_systemctl([["disable", "--now", unit_name]])
    unit_path.unlink(missing_ok=True)
    _, reload_errors = _run_user_systemctl([["daemon-reload"]])
    try:
        record = json.loads(paths.supervisor_service.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("unit_name") == unit_name:
            paths.supervisor_service.unlink(missing_ok=True)
    except (FileNotFoundError, ValueError):
        pass
    return {"unit_name": unit_name, "removed": True, "stopped": attempted and not errors,
            "errors": errors + reload_errors}


# LLM: 输出保留systemctl失败，CLI非零不等于unit未写入，避免误导重复安装。
# 函数用途: 显示服务安装/卸载回执并给出对应退出码。
def _print_service_result(result: dict) -> int:
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["errors"] else 0


# LLM: CLI唯一make_agent选择同配置与home；不依据当前目录猜服务归属。
# 函数用途: 将命令行安装参数交给用户级服务安装器。
def cmd_supervisor_install_service(args) -> int:
    agent = make_agent(args)
    result = install_supervisor_systemd_service(gateway_paths(agent), config_path=agent.config.config_path,
        home_root=agent.home_paths.root, unit_name=args.unit, start_now=not args.no_start)
    return _print_service_result(result)


# LLM: 卸载使用同一Agent的规范回执路径；只处理显式选择的unit。
# 函数用途: 卸载OS看门狗，不直接停止Gateway进程。
def cmd_supervisor_uninstall_service(args) -> int:
    agent = make_agent(args)
    return _print_service_result(uninstall_supervisor_systemd_service(gateway_paths(agent), unit_name=args.unit))


# LLM: 维护只发布有限期意图，不停止服务或修改业务状态。
# 函数用途: 为所选Gateway写入CLI维护锁并显示回执。
def cmd_maintenance_lock(args) -> int:
    agent = make_agent(args)
    lock = write_maintenance_lock(gateway_paths(agent), reason=args.reason, ttl_seconds=args.ttl)
    print(json.dumps(lock, ensure_ascii=False))
    return 0


# LLM: 解锁幂等且仅作用于所选配置，恢复由后续原监护节奏触发。
# 函数用途: 移除维护锁并显示是否已解除。
def cmd_maintenance_unlock(args) -> int:
    agent = make_agent(args)
    print(json.dumps({"cleared": clear_maintenance_lock(gateway_paths(agent))}))
    return 0


# LLM: 原daemon入口保留当前解释器，OS服务安装才选择稳定启动器。
# 函数用途: 构造前台监护子命令供后台进程调用。
def _supervisor_daemon_cmd(config: str) -> list[str]:
    """supervisor 守护进程的启动命令(前台跑 gateway supervisor 循环)。
    supervisor-start 和 start-all 两处共用,防再写成不一致(曾 start-all 漏 gateway→看门狗起不来)。"""
    return [sys.executable, "-m", "agent_py_agent", "--config", config, "gateway", "supervisor"]


# LLM: 原daemon创建副作用保持；调用方须先验证角色与目录。
# 函数用途: 脱离终端启动子进程并输出到日志。
def _spawn_daemon(cmd: list[str], log_path: Path) -> subprocess.Popen:
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            cmd, cwd=ROOT.parent, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=start_new_session,
        )


# LLM: start-all运输已解析原Agent与原paths，不能用占位对象或默认N；复用监护同角色/身份/dispatcher及全worker健康读门。
# 函数用途: 在原预算内等待真实Gateway服务就绪，不启动额外探针或写状态。
def _wait_for_gateway_ready(paths, agent, timeout: float = 30.0) -> tuple[int, bool] | None:
    print("等待 gateway 就绪...")
    supervisor = GatewaySupervisor(agent.config.config_path, agent=agent)
    supervisor._paths = paths
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if supervisor._is_gateway_healthy():
            report = get_running_pid_report(paths.pid, cleanup_stale=False)
            if report.pid is not None and report.load_error is None:
                return report.pid, True
        time.sleep(1.0)
    return None


# LLM: daemon启动确认只认本次Popen同PID且真实生年代已验的supervisor记录；旧数字PID不能冒充成功。
# 函数用途: 只读确认本次监护进程，不清理或补写身份。
def _pid_file_matches(pid_path: Path, expected_pid: int) -> bool | None:
    report = read_supervisor_process_report(pid_path)
    value = report.payload or {}
    if report.load_error is not None or not value:
        return None
    return value.get("pid") == expected_pid and value.get("alive") is True and value.get("identity_verified") is True


# LLM: 只读等待本次Popen的同代身份，超时不得宣称已运行。
# 函数用途: 在期限内确认监护启动记录。
def _wait_for_supervisor_start(supervisor_pid_path: Path, process: subprocess.Popen, deadline: float) -> bool:
    while time.time() < deadline:
        if _pid_file_matches(supervisor_pid_path, process.pid) is True:
            return True
        time.sleep(0.2)
    return False


# LLM: 原配置/身份预核先行，确认可启动后才沿原可信配置解析的service路径文件系统锚点逐段私有准备；未知记录不建目录/重复spawn，不补身份。
#   文件系统锚点仅作可信配置路径的no-follow创建起点，不认证owner；先拒上级跳转，防准备和原log打开不同链。
# 函数用途: 为首次监护启动准备规范日志父目录，再创建进程并等待其独立身份回执；既有目录权限不改。
def cmd_supervisor_start(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if is_supervisor_running(paths):
        print("supervisor 已有运行记录或身份待核验；未重复启动，请用 gateway supervisor-status 核验。")
        return 0

    if ".." in paths.root.parts:
        raise ValueError("Gateway服务目录不能含上级跳转；请配置不含..的规范绝对服务目录。")
    ensure_private_dir(paths.root, canonical_root=paths.root.anchor)
    supervisor_pid_path = paths.supervisor_pid
    cmd = [sys.executable, "-m", "agent_py_agent", "--config", args.config, "gateway", "supervisor"]
    process = _spawn_daemon(cmd, paths.log)

    deadline = time.time() + 10.0
    if _wait_for_supervisor_start(supervisor_pid_path, process, deadline):
        print(f"supervisor starting pid={process.pid}")
    else:
        print(f"supervisor 身份未确认 pid={process.pid}；请核状态，勿重复启动。", file=sys.stderr)
        return 2
    print(f"workspace: {paths.root}")
    return 0


# LLM: 唯一CLI组合根沿make_agent解析YAML/环境/owner与工作区一次，runtime只消费这个真实Agent，不能注入任意factory或Count0默认。
# 函数用途: 装配实际Agent后进入原监护循环，配置失败仍沿原CLI错误出口。
def cmd_supervisor_run(args) -> int:
    try:
        agent = make_agent(args)
        return run_supervisor(
            agent.config.config_path,
            agent=agent,
            heartbeat_timeout=getattr(args, "heartbeat_timeout", 120.0),
            check_interval=getattr(args, "check_interval", 10.0),
            max_restart_attempts=getattr(args, "max_restart_attempts", 5),
            restart_cooldown=getattr(args, "restart_cooldown", 30.0),
        )
    except Exception as exc:
        print(f"supervisor error: {exc}", file=sys.stderr)
        return 2


# LLM: 状态命令复用make_agent所得原Agent/配置/paths核实际N，不清PID或另建Agent；supervisor活不等于Gateway ready。
# 函数用途: 分别显示监护进程与原Gateway的可核验健康，不把角色状态混用。
def cmd_supervisor_status(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    report = read_supervisor_process_report(paths.supervisor_pid)
    value = report.payload or {}
    if report.load_error is not None:
        print("supervisor: 身份未确认，请核对同一配置后正常停止并重新启动。")
    elif value.get("alive") is True:
        print(f"supervisor: 运行中 pid={value['pid']}")
    else:
        print("supervisor: 未运行")
    supervisor = GatewaySupervisor(agent.config.config_path, agent=agent)
    supervisor._paths = paths
    if supervisor._is_gateway_healthy():
        print(f"gateway: 已就绪 pid={get_running_pid_report(paths.pid, cleanup_stale=False).pid}")
    else:
        print("gateway: 未确认就绪，请用 gateway status 核验。")
    print(f"workspace: {paths.root}")
    return 0


# LLM: 唯一CLI组合根先解析实际Agent，再把其规范路径交原停止函数；不在runtime重建Agent或改变目标PID/等待门。
# 函数用途: 停止所选配置对应的监护进程，保留原返回码和超时。
def cmd_supervisor_stop(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if stop_supervisor(paths, timeout=args.timeout or 10.0):
        print("supervisor 已停止")
        return 0
    else:
        print("supervisor 停止超时", file=sys.stderr)
        return 2


# LLM: 只构造一次正式Agent，原paths/配置连同Agent运输给ready reader；保留原启动顺序及适配器副作用，不放宽健康门。
# 函数用途: 沿原监护启动与Gateway就绪后再启动所选适配器，避免健康读取丢失实际拓扑配置。
def cmd_start_all(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    adapter_channel = getattr(args, "adapter_channel", getattr(args, "adapter", "all"))

    if is_supervisor_running(paths):
        print("supervisor 已有运行记录或身份待核验；未重复启动，请用 gateway supervisor-status 核验。")
    else:
        cmd = _supervisor_daemon_cmd(args.config)
        _spawn_daemon(cmd, paths.log)
        print("supervisor 启动中...")

    result = _wait_for_gateway_ready(paths, agent)
    if result is None:
        print("gateway 启动超时，请检查日志", file=sys.stderr)
        return 2
    gpid, _ = result
    print(f"gateway 就绪: pid={gpid}")

    if adapter_channel not in (None, 'none'):
        print(f"启动通道适配器: {adapter_channel}")
        adapter_cmd = [sys.executable, "-m", "agent_py_agent", "--config", args.config,
                       "adapter", "start", "--channel", adapter_channel, "--daemon"]
        _spawn_daemon(adapter_cmd, paths.log)
        print("适配器启动中...")

    print("start-all 完成。运行 `my-agent gateway status` 查看状态。")
    return 0
