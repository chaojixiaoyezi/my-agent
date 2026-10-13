# LLM: 私有离线发行的安装器；只管理显式 prefix 下的版本、入口和初始化数据，升级不覆盖数据。
# 模块用途: 用发行包自带 Python 完成校验、首次安装和原子版本切换，不依赖登录 HOME 或网络。
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = "my-agent.private-release.v1"
MARKER = ".my-agent-private-install.json"


# LLM: 默认工作目录只由产品 owner resolver 决定；安装器不得另建 owner home 外的工作区或扩大权限。
# 函数用途: 返回该安装下本地主用户的默认工作目录，保持 WorkspaceOnly 和原 TUI 首装语义。
def workspace_path(prefix: Path) -> Path:
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

    return resolve_owner_home(prefix / "data").home_dir


# LLM: 校验所有发行成员及链接目标；manifest 是构建记录，不读取任何用户运行目录。
# 函数用途: 安装前检查完整性、架构与发行包格式，坏包不能切换 current。
def verify(root: Path) -> dict:
    manifest = json.loads((root / "release.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or not re.fullmatch(r"[A-Za-z0-9_.-]+", manifest["version"]):
        raise ValueError("不支持的发行包格式或版本")
    if os.uname().machine != manifest["architecture"]:
        raise ValueError("安装包 CPU 架构与本机不匹配")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != set(manifest["files"]) | {"release.json"}:
        raise ValueError("发行成员清单不完整或包含额外文件")
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"发行成员越界: {relative}")
        if "link" in expected:
            if not path.is_symlink() or os.readlink(path) != expected["link"]:
                raise ValueError(f"发行链接损坏: {relative}")
        elif not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"发行文件校验失败: {relative}")
    return manifest


# LLM: 只查看本安装目录所属进程的 exe/cwd 身份；不读取命令行、任务正文或其它应用状态。
# 函数用途: 阻止活动进程期间切换版本，避免新 TUI 连接旧 Gateway；不杀进程。
def active_processes(prefix: Path) -> list[int]:
    running = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if executable.is_relative_to(prefix / "releases"):
            running.append(int(entry.name))
    return sorted(running)


# LLM: 启动入口固定解析安装记录；数据与配置是持久路径，-B 显式禁止隔离解释器在不可变版本写缓存。
# 函数用途: 生成可在任意工作目录调用的启动器，用户的 HOME 不参与后续升级目标选择。
def launcher_text(prefix: Path) -> str:
    quoted = shlex.quote(str(prefix))
    return f'''#!/usr/bin/env bash
set -euo pipefail
prefix={quoted}
release="$(readlink -f "$prefix/current")"
export MY_AGENT_HOME="$prefix/data"
export MY_AGENT_CONFIG="$prefix/data/config/agent_config.yaml"
export TMPDIR="$prefix/data/tmp"
export XDG_CACHE_HOME="$prefix/data/cache"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1
export PATH="$release/runtime/bin:$PATH:$release/native"
if [ -z "${{SSL_CERT_FILE:-}}" ]; then
  if [ -f /etc/ssl/certs/ca-certificates.crt ]; then export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  else export SSL_CERT_FILE="$release/native/ca-certificates.crt"; fi
fi
unset PYTHONHOME PYTHONPATH
if [ "${{1:-}}" = "--release-info" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 -c 'import json,sys; d=json.load(open(sys.argv[1])); d.pop("files"); print(json.dumps(d,ensure_ascii=False,indent=2))' "$release/release.json"
fi
if [ "${{1:-}}" = "--version" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 -c 'import json,sys; print("my-agent " + json.load(open(sys.argv[1]))["version"])' "$release/release.json"
fi
if [ "${{1:-}}" = "--configure-model" ]; then
  exec "$release/runtime/bin/python3" -I -B -X utf8 "$release/configure.pyc" "$MY_AGENT_CONFIG"
fi
exec "$release/runtime/bin/python3" -I -B -X utf8 -m agent_py_agent "$@"
'''


# LLM: 首装保留 canonical YAML 的空 workspace_root，由原 owner resolver 选目录；更新不覆盖持久数据。
# 函数用途: 在独立数据目录初始化空白配置与工作区，不带开发者账号或测试模型凭据。
def initialize_data(prefix: Path, release: Path) -> None:
    config = prefix / "data" / "config" / "agent_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (prefix / "data/tmp").mkdir(exist_ok=True, mode=0o700)
    (prefix / "data/cache").mkdir(exist_ok=True, mode=0o700)
    workspace_path(prefix).mkdir(parents=True, exist_ok=True)
    if config.exists():
        return
    package = release / "runtime" / "lib" / "python3.11" / "site-packages" / "agent_py_agent"
    content = (package / "config" / "agent_config.yaml").read_text(encoding="utf-8")
    # 空工作区配置保留原 owner-home 权威；环境绑定 data 后无需另建外部目录或放宽权限。
    content = re.sub(r'^gateway_port:.*$', 'gateway_port: 18420', content, flags=re.M)
    with config.open("x", encoding="utf-8") as stream:
        stream.write(content)
    config.chmod(0o600)


# LLM: 修改范围限于经过标记的安装目录；版本冲突、校验失败及活动进程均在切换前失败。
# 函数用途: 安装不可变版本，保留旧版本及用户数据，最后原子切换 current。
def install(bundle: Path, prefix: Path) -> dict:
    manifest = verify(bundle)
    prefix = prefix.expanduser().resolve()
    if any(character in str(prefix) for character in "\n\r\0"):
        raise ValueError("安装路径不能包含控制字符")
    prefix.mkdir(parents=True, exist_ok=True)
    if prefix.stat().st_uid != os.getuid():
        raise ValueError("安装目录属于另一个账户，请使用目录所属账户安装和更新")
    marker = prefix / MARKER
    if not marker.exists() and any(path.name != ".install.lock" for path in prefix.iterdir()):
        raise ValueError("目标目录非空且不属于本安装器，请使用空目录")
    with (prefix / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if pids := active_processes(prefix):
            raise ValueError(f"请先退出本安装的 TUI 并停止 Gateway 后重试；活动 PID: {pids}")
        record = {"schema": SCHEMA, "prefix": str(prefix), "data": str(prefix / "data"), "workspace": str(workspace_path(prefix)), "owner_uid": os.getuid()}
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != record:
            raise ValueError("安装记录与目标路径不一致，请不要直接搬移运行中的安装目录")
        marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        releases = prefix / "releases"
        releases.mkdir(exist_ok=True)
        target = releases / manifest["version"]
        if target.exists():
            if (target / "release.json").read_bytes() != (bundle / "release.json").read_bytes():
                raise ValueError("同版本已有不同内容，必须使用新的发行版本号")
            verify(target)
        else:
            staging = releases / f".staging-{os.getpid()}"
            try:
                shutil.copytree(bundle, staging, symlinks=True)
                verify(staging)
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        initialize_data(prefix, target)
        (prefix / "bin").mkdir(exist_ok=True)
        launcher = prefix / "bin" / "my-agent-preview"
        temporary = launcher.with_name(f".launcher-{os.getpid()}")
        temporary.write_text(launcher_text(prefix), encoding="utf-8")
        temporary.chmod(0o755)
        os.replace(temporary, launcher)
        alias = prefix / "bin" / "my-agent"
        if not alias.exists():
            alias.symlink_to("my-agent-preview")
        current = prefix / f".current-{os.getpid()}"
        current.symlink_to(Path("releases") / manifest["version"])
        os.replace(current, prefix / "current")
    return {"version": manifest["version"], **record}


# LLM: CLI 是唯一安装入口；仅打印原 gateway start 与 TUI 命令，不在安装时启动后台或联网。
# 函数用途: 执行离线安装并打印启动、模型配置与更新命令。
# LLM: 实例发现只认同 uid 的原安装标记，不递归扫描业务目录；自定义安装从环境/PATH 定位，多实例不猜。
# 函数用途: 查找当前用户已有离线安装，自动更新原 prefix，输入错目录时明确拒绝第二套实例。
def discover_installations(roots=None) -> list[Path]:
    roots = list(roots) if roots is not None else [Path('/app'), Path('/opt'), Path.home()]
    candidates = []
    configured = os.environ.get('MY_AGENT_HOME')
    if configured:
        candidates.append(Path(configured).expanduser().parent)
    for name in ('my-agent', 'my-agent-preview'):
        if executable := shutil.which(name):
            candidates.append(Path(executable).resolve().parent.parent)
    for root in roots:
        if not root.is_dir():
            continue
        candidates.append(root)
        candidates.extend(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    found = set()
    for path in candidates:
        marker = path / MARKER
        if not marker.is_file() or marker.is_symlink():
            continue
        try:
            row = json.loads(marker.read_text(encoding='utf-8'))
            resolved = path.resolve()
            if row.get('schema') == SCHEMA and row.get('owner_uid') == os.getuid() and row.get('prefix') == str(resolved) and path.stat().st_uid == os.getuid():
                found.add(resolved)
        except (OSError, ValueError):
            continue
    return sorted(found)


# LLM: 自动更新唯一原实例；显式不同路径也不能意外建第二套，多实例时必须选择已登记 prefix。
# 函数用途: 决定首装或更新的准确目录，目录检查失败时还未停服务或写磁盘。
def resolve_install_prefix(requested: Path | None, *, roots=None) -> Path:
    found = discover_installations(roots)
    selected = requested.expanduser().resolve() if requested is not None else None
    if selected is not None:
        if found and selected not in found:
            raise ValueError('发现已有安装：' + ', '.join(str(path) for path in found) + '；请更新原目录，不能自动建立第二个 Gateway')
        return selected
    if len(found)>1:
        raise ValueError('发现多个安装，请用 --prefix 精确选择：' + ', '.join(str(path) for path in found))
    return found[0] if found else Path('/app/.my-agent')


# LLM: 使用原 CLI 停止/恢复准确实例；不按名字 kill python、不动数据。TUI 或旧进程未退出时仍禁止切换。
# 函数用途: 更新前保存原后台服务状态，沿既有命令停止，切换成功或失败后恢复原来开启的服务。
def update_services(prefix: Path, *, restore: tuple[bool,bool,str] | None = None) -> tuple[bool,bool,str]:
    import subprocess

    from agent_py_agent.agent.gateway_parts import is_pid_alive
    from agent_py_agent.agent.gateway_parts.daemon_control import get_running_pid
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import load_config

    launcher = str(prefix / 'bin' / 'my-agent-preview')
    config = load_config(prefix / 'data' / 'config' / 'agent_config.yaml')
    paths = gateway_paths_from_root(workspace_path(prefix) / config.gateway_workspace)
    # LLM: PID 是原服务记录，只用生存事实，不以状态文案猜服务是否运行。
    # 函数用途: 核对原实例当前服务 PID。
    def alive(path):
        pid = get_running_pid(path)
        return bool(pid and is_pid_alive(pid))
    adapter_alive=alive(paths.adapter_pid) if restore is None else restore[1]
    channel='all'
    if restore is None and adapter_alive:
        state=json.loads((paths.root / 'adapter_state.json').read_text(encoding='utf-8'))
        channel=state.get('requested_channel')
        if channel not in {'feishu','qq','all'}:
            raise ValueError('原适配器通道记录不可用，未停止服务')
    original = restore if restore is not None else (alive(paths.pid), adapter_alive, channel)
    commands = ([['gateway','start','--ready-timeout','60']] if original[0] else []) + ([['adapter','start','--daemon','--channel',original[2]]] if original[1] else [])
    if restore is None:
        release = json.loads((prefix / 'current' / 'release.json').read_text(encoding='utf-8'))
        drain = ['--drain'] if (release.get('protocols') or {}).get('offline_update') == 'drain.v1' else []
        commands = ([['adapter','stop']] if original[1] else []) + [['gateway','stop',*drain,'--stop-background','--background-timeout','30']]
    try:
        for arguments in commands:
            result = subprocess.run([launcher,*arguments], timeout=6000, check=False)
            if result.returncode:
                raise ValueError('原实例服务控制未完成：' + ' '.join(arguments[:2]))
    except (OSError,ValueError,subprocess.SubprocessError):
        if restore is None:
            update_services(prefix, restore=original)
        raise
    return original


# LLM: 安装切换复用原目录/服务排空；任何 subprocess 超时必须作为安装错误返回，恢复路径仍由原服务控制负责。
# 函数用途: 校验离线包、更新原实例并恢复连接，对超时或状态异常给出明确失败，不能宣称更新成功。
def main() -> int:
    parser = argparse.ArgumentParser(description="my-agent 临时离线安装 / 更新")
    parser.add_argument("--prefix", type=Path, help="省略时识别原实例并更新；首装默认 /app/.my-agent")
    parser.add_argument("--update", action="store_true", help="退出 TUI 后自动停止原实例后台并在更新后恢复原连接")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    try:
        if args.verify_only:
            print(json.dumps({"verified": verify(bundle)["version"]}, ensure_ascii=False))
            return 0
        prefix = resolve_install_prefix(args.prefix)
        verify(bundle)
        services = None
        try:
            if (prefix / MARKER).is_file():
                assert_interactive_processes_exited(prefix)
                services = update_services(prefix)
            result = install(bundle, prefix)
        finally:
            if services is not None:
                update_services(prefix, restore=services)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"安装失败：{error}", file=sys.stderr)
        return 1
    command = shlex.quote(str(Path(result["prefix"]) / "bin" / "my-agent-preview"))
    print(f"安装完成：{result['version']}\n程序及更新目录：{result['prefix']}\n数据目录：{result['data']}")
    print(f"首次配置模型：{command} --configure-model\n启动后台：{command} gateway start --ready-timeout 30\n进入 TUI：{command}")
    print(f"更新：退出 TUI 后，执行 {command} update --package 新包.run；也可 bash 新包.run --update（自动识别原目录）")
    return 0



# LLM: 只读本安装 PID 的 tty 事实，不读取任务正文或向终端发送退出键；确认失败在停服务之前拒绝更新。
# 函数用途: 要求 TUI/交互进程先退出，避免自动更新途中误关正在输入的用户。
def assert_interactive_processes_exited(prefix):
    for pid in active_processes(prefix):
        try:
            state=Path(f'/proc/{pid}/stat').read_text().split(')',1)[1].split()
            if int(state[4])!=0:
                raise ValueError(f'请先退出本安装 TUI/交互进程再更新：PID {pid}')
        except (FileNotFoundError,ProcessLookupError):
            continue

if __name__ == "__main__":
    raise SystemExit(main())
