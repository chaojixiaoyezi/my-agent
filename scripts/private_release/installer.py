# LLM: 仅管理显式prefix的安装与维护；进程清理限定同uid、无tty及已核出生身份，不覆盖数据或改动其它安装。
# 模块用途: 离线校验和原子升级，先暂停守望与排空服务，再精准清理本安装遗留脚本，最后恢复原服务。
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
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


# LLM: 只按本安装releases下真实exe发现PID；stat或命令行不可读也不能绕过最后安装阻断，不发信号。
# 函数用途: 兼容返回排序PID集合；无proc的平台没有Linux观察结果。
def active_processes(prefix: Path) -> list[int]:
    running = []
    try:
        entries = list(Path('/proc').iterdir())
    except FileNotFoundError:
        return running
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if executable.is_relative_to(prefix / "releases"):
            running.append(int(entry.name))
    return sorted(running)


# LLM: 只消费本prefix exe发现结果；读不到stat的PID不猜为后台，最后active_processes仍拒绝版本切换。
# 函数用途: 返回按PID排序的父进程、终端与完整命令行诊断，不发信号。
def inspect_active_processes(prefix: Path) -> list[dict]:
    rows = [_process_details(pid) for pid in active_processes(prefix)]
    return sorted((row for row in rows if row is not None), key=lambda row: row['pid'])


# LLM: cmdline仅供诊断，不能作为安装身份；读失败为空，NUL分隔按proc协议转空格，不读取环境或文件内容。
# 函数用途: 读取一个进程公开的命令参数以展示遗留脚本来源。
def _process_cmdline(pid):
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', errors='replace').strip()
    except OSError:
        return ''


# LLM: stat从comm的最后右括号取内核位置；无法读出的样本不当无终端，父命令行可缺失。
# 函数用途: 取得一条公开进程详情，供安装诊断与脚本父进程检查。
def _process_details(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        ppid, tty = int(state[1]), int(state[4])
    except (OSError, ValueError, IndexError):
        return None
    return {'pid': pid, 'ppid': ppid, 'tty_nr': tty, 'interactive': tty != 0,
        'cmdline': _process_cmdline(pid), 'parent_cmdline': _process_cmdline(ppid)}


# LLM: 出生时间、exe、uid与tty一起固定，comm可含右括号；发送和升级前均重核，缺证据不发信号。
# 函数用途: 捕获Linux进程的准确一代身份，退出或不可核验时返回None。
def _process_identity(pid):
    try:
        entry = Path(f'/proc/{pid}')
        state = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
        return (entry.stat().st_uid, str((entry / 'exe').resolve(strict=True)), state[19], int(state[4]))
    except (OSError, ValueError, IndexError):
        return None


# LLM: 只解析不带命令串/输入选项的shell首个脚本参数；绝对位置或cwd解析后须是本prefix的真实文件。
# 函数用途: 证明包装脚本归属，不把共享批处理或普通.sh参数当成本安装专用脚本。
def _wrapper_identity(pid, prefix):
    if pid <= 1 or pid in (os.getpid(), os.getppid()):
        return None
    identity = _process_identity(pid)
    if identity is None or identity[0] != os.getuid() or identity[3] != 0 or Path(identity[1]).name not in ('bash', 'sh', 'dash'):
        return None
    try:
        arguments = [value.decode('utf-8') for value in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[1:] if value]
        while arguments and re.fullmatch(r'-[euxv]+', arguments[0]):
            arguments.pop(0)
        if arguments and arguments[0] == '--':
            arguments.pop(0)
        if not arguments or arguments[0].startswith('-'):
            return None
        script = Path(arguments[0])
        if not script.is_absolute():
            script = Path(f'/proc/{pid}/cwd').resolve(strict=True) / script
        script = script.resolve(strict=True)
    except OSError:
        return None
    return identity if script.is_relative_to(prefix) and script.is_file() else None


# LLM: 原观察须仍属同uid本prefix且无tty，父子关系在父身份捕获前后重核；旧PPID不授予停止权。
# 函数用途: 汇总本安装遗留子进程及可确认的shell父进程，不扩展到整进程组。
def _orphan_targets(prefix, rows):
    targets = {}
    for row in rows:
        identity = _process_identity(row['pid'])
        if (identity is None or identity[0] != os.getuid() or identity[3] != 0
                or not Path(identity[1]).is_relative_to(prefix / 'releases')):
            continue
        current = _process_details(row['pid'])
        parent = _wrapper_identity(row['ppid'], prefix) if current and current['ppid'] == row['ppid'] else None
        after = _process_details(row['pid'])
        if parent is not None and after and after['ppid'] == row['ppid'] and _process_identity(row['pid']) == identity:
            targets.setdefault(row['ppid'], parent)
        targets[row['pid']] = identity
    return targets


# LLM: 仅用pidfd绑定进程并重核身份后发送，缺安全句柄拒绝自动清理；不回落存在复用竞态的裸PID信号。
# 函数用途: 给原一代进程发TERM或KILL，退出/换代无信号，旧平台保留诊断而不误杀。
def _signal_process(pid, identity, sig):
    if _process_identity(pid) != identity:
        return
    try:
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise ValueError(f'缺少安全pidfd进程句柄，未自动终止 PID {pid}，请手动停止后重试')
        descriptor = os.pidfd_open(pid)
        try:
            if _process_identity(pid) == identity:
                signal.pidfd_send_signal(descriptor, sig)
        finally:
            os.close(descriptor)
    except ProcessLookupError:
        pass


# LLM: 单调时钟有界等待已固定身份消失，PID复用不是原进程仍活；不借等待扩大清理集合。
# 函数用途: 为优雅退出和强制退出各留出调用方指定的超时窗口。
def _wait_for_processes(targets, timeout):
    deadline = time.monotonic() + timeout
    while any(_process_identity(pid) == identity for pid, identity in targets.items()):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


# LLM: 仅在原服务stop成功后调用；先同uid无tty包装器再子进程，超时才KILL，未退出不谎报成功。
# 函数用途: 清理本安装残留旧解释器并打印PID/PPID/命令行，返回原诊断供更新回执使用。
def cleanup_orphan_background_processes(prefix: Path, *, timeout: float = 5.0) -> list[dict]:
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('进程清理超时必须是非负有限数')
    prefix = prefix.resolve()
    rows = [row for row in inspect_active_processes(prefix) if not row['interactive']]
    targets = _orphan_targets(prefix, rows)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, identity in targets.items():
            _signal_process(pid, identity, sig)
        _wait_for_processes(targets, timeout)
    remaining = [pid for pid, identity in targets.items() if _process_identity(pid) == identity]
    if remaining:
        raise ValueError(f'遗留后台进程清理未完成，活动 PID: {remaining}')
    cleaned = [row for row in rows if row['pid'] in targets]
    for row in cleaned:
        print(f"已清理遗留后台进程：PID {row['pid']} PPID {row['ppid']} ({row['cmdline'] or 'unknown'})")
    return cleaned


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


# LLM: 修改范围限于标记安装目录；校验、版本冲突及活动PID均在切换前拒绝，新增详情不改变旧错误前缀。
# 函数用途: 保留数据并原子切换不可变版本；阻断时展示PID/PPID/命令以定位遗留脚本。
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
            details = '; '.join(f"PID {row['pid']} PPID {row['ppid']} ({row['cmdline'] or 'unknown'})"
                for row in inspect_active_processes(prefix))
            raise ValueError(f"请先退出本安装的 TUI 并停止 Gateway 后重试；活动 PID: {pids}；详情: {details}")
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
        # 显式更新深层自定义 prefix 仍核对同一标记合同，不能因未列在浅层发现根中误当成第二套安装。
        found = sorted(set(found) | set(discover_installations([selected])))
        if found and selected not in found:
            raise ValueError('发现已有安装：' + ', '.join(str(path) for path in found) + '；请更新原目录，不能自动建立第二个 Gateway')
        return selected
    if len(found)>1:
        raise ValueError('发现多个安装，请用 --prefix 精确选择：' + ', '.join(str(path) for path in found))
    return found[0] if found else Path('/app/.my-agent')


# LLM: 仅复用原owner/runtime解析器，安装器环境不覆盖持久prefix；不创建第二套服务目录。
# 函数用途: 找到准确安装的Gateway服务路径，供维护锁、原服务与Supervisor共用。
def _update_service_paths(prefix):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import load_config
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.home_root import configured_home_root
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    config = load_config(prefix / 'data' / 'config' / 'agent_config.yaml')
    base = home_paths(configured_home_root(config, env={'MY_AGENT_HOME': str(prefix / 'data')}))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    scoped = home_paths_with_owner(base, owner)
    return gateway_paths_from_root(runtime_paths_for_agent(config, owner.home_dir, scoped)['gateway_workspace'])


# LLM: 只读原PID记录及内核生存事实，复用原身份检查，不从状态文案猜活进程。
# 函数用途: 确认更新前某个准确服务是否在运行。
def _service_alive(path):
    from agent_py_agent.agent.gateway_parts import is_pid_alive
    from agent_py_agent.agent.gateway_parts.daemon_control import get_running_pid

    pid = get_running_pid(path)
    return bool(pid and is_pid_alive(pid))


# LLM: 维护锁原子发表，固定schema/TTL；恢复先撤锁，不把服务启动失败伪装成成功或永久维护。
# 函数用途: 让守望进程在离线更新期间暂停，恢复时忽略原锁已不存在。
def _maintenance_lock(root, *, remove=False):
    target = root / 'maintenance_lock.json'
    if remove:
        target.unlink(missing_ok=True)
        return
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    payload = {'schema': 'my-agent.maintenance-lock.v1', 'reason': 'offline_update', 'actor': 'installer',
        'created_at': now, 'expires_at': now + 1800, 'pid': os.getpid()}
    temporary = root / f'.maintenance-lock-{os.getpid()}.tmp'
    try:
        temporary.write_text(json.dumps(payload) + '\n', encoding='utf-8')
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# LLM: systemd单元名仅来自对象元数据或固定默认，写锁/停服务前拒绝坏类型；无systemctl不尝试用户服务。
# 函数用途: 读取Supervisor安装单元名，并查询本用户单元是否活动。
def _supervisor_unit_state(root):
    record = root / 'supervisor_service.json'
    unit = _update_json_object(record).get('unit_name', 'my-agent-gw-supervisor.service') if record.exists() else 'my-agent-gw-supervisor.service'
    if not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service', unit):
        raise ValueError('Supervisor systemd单元名无效')
    active = shutil.which('systemctl') is not None and subprocess.run(
        ['systemctl', '--user', 'is-active', '--quiet', unit], timeout=30, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return active, unit


# LLM: 更新控制只接受JSON对象，不将数组/标量变成默认状态；在任何服务停止前校验。
# 函数用途: 读取发行协议和Supervisor控制元数据，坏记录明确拒绝。
def _update_json_object(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('更新元数据必须是JSON对象：' + str(path))
    return value


# LLM: 仅控制已确认属于原Supervisor的用户单元，任何停启失败都阻止把升级称为成功。
# 函数用途: 沿systemctl暂停或恢复更新前已运行的守望服务。
def _control_supervisor_unit(action, unit):
    result = subprocess.run(['systemctl', '--user', action, unit], timeout=60, check=False)
    if result.returncode:
        raise ValueError('Supervisor用户服务控制未完成：' + action + ' ' + unit)


# LLM: 原gateway/adapter控制失败仍硬拒绝；仅supervisor-stop允许旧CLI无此命令，后续清理和安装核验不放宽。
# 函数用途: 串行执行准确安装启动器的服务动作，不使用shell或按名字杀进程。
def _run_update_commands(prefix, commands):
    launcher = str(prefix / 'bin' / 'my-agent-preview')
    for arguments in commands:
        result = subprocess.run([launcher, *arguments], timeout=6000, check=False)
        if result.returncode and arguments[:2] != ['gateway', 'supervisor-stop']:
            raise ValueError('原实例服务控制未完成：' + ' '.join(arguments[:2]))
        if result.returncode:
            print('Supervisor停止命令未成功，继续原服务排空与遗留进程核验（旧发行可能不支持该命令）')


# LLM: 独立尝试每个原活跃组件，收集错误后一起报；准确维护根由停止调用栈或当前原路径解析传入。
# 函数用途: 先撤维护锁再恢复原连接和守望，一个组件失败不丢掉其它恢复义务。
def _restore_update_services(prefix, root, original):
    _maintenance_lock(root, remove=True)
    commands = ([['gateway', 'start', '--ready-timeout', '60']] if original[0] else []) + (
        [['adapter', 'start', '--daemon', '--channel', original[2]]] if original[1] else [])
    operations = [('cli', arguments) for arguments in commands]
    if len(original) > 3:
        supervisor_alive, unit_active, unit = original[3:]
        if unit_active:
            operations.append(('systemd', unit))
        elif supervisor_alive:
            operations.append(('cli', ['gateway', 'supervisor-start']))
    errors = []
    for kind, value in operations:
        try:
            if kind == 'systemd':
                _control_supervisor_unit('start', value)
            else:
                _run_update_commands(prefix, [value])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
    if errors:
        raise ValueError('原实例服务恢复未完成：' + '；'.join(errors))
    return original


# LLM: 原三元组兼容；Supervisor活跃才扩展六元组，配置先验、锁先于stop；任意中断沿原已捕获root恢复，不重解析权限。
# 函数用途: 暂停本安装守望与服务，清理遗留进程；失败仍撤维护锁并尝试所有原恢复义务。
def update_services(prefix: Path, *, restore: tuple | None = None) -> tuple:
    paths = _update_service_paths(prefix)
    if restore is not None:
        return _restore_update_services(prefix, paths.root, restore)
    adapter_alive = _service_alive(paths.adapter_pid)
    channel = restore_channel(json.loads((paths.root / 'adapter_state.json').read_text(encoding='utf-8'))) if adapter_alive else 'all'
    original = (_service_alive(paths.pid), adapter_alive, channel)
    supervisor_alive = _service_alive(paths.root / 'supervisor.pid')
    unit_active, unit = _supervisor_unit_state(paths.root)
    release = _update_json_object(prefix / 'current' / 'release.json')
    protocols = release.get('protocols')
    if protocols is None:
        protocols = {}
    if not isinstance(protocols, dict):
        raise ValueError('发行包protocols必须是JSON对象')
    if supervisor_alive or unit_active:
        original = (*original, supervisor_alive, unit_active, unit)
    _maintenance_lock(paths.root)
    try:
        if unit_active:
            _control_supervisor_unit('stop', unit)
        drain = ['--drain'] if protocols.get('offline_update') == 'drain.v1' else []
        commands = ([['gateway', 'supervisor-stop']] if supervisor_alive else []) + (
            [['adapter', 'stop']] if adapter_alive else []) + [['gateway', 'stop', *drain, '--stop-background', '--background-timeout', '30']]
        _run_update_commands(prefix, commands)
        cleanup_orphan_background_processes(prefix)
    except BaseException as error:
        try:
            _restore_update_services(prefix, paths.root, original)
        except (OSError, ValueError, subprocess.SubprocessError) as restore_error:
            raise ValueError(f'更新停止未完成：{error}；恢复亦未完成：{restore_error}') from error
        raise
    return original


# LLM: 旧发行的周期状态没有 requested_channel，显式从原 registry channels 快照迁移；非法/冲突记录在停服务前拒绝。
# 函数用途: 从结构化启动参数或已注册通道确定恢复范围，不读取命令行正文、不把未知通道猜成 all。
def restore_channel(state: dict) -> str:
    if not isinstance(state, dict):
        raise ValueError('原适配器通道记录不可用，未停止服务')
    rows = state.get('channels')
    names = [row.get('name') for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not names or len(names) != len(rows) or any(not isinstance(name, str) or name not in {'feishu', 'qq'} for name in names) or len(names) != len(set(names)):
        raise ValueError('原适配器通道记录不可用，未停止服务')
    requested = state.get('requested_channel')
    if requested is not None:
        if not isinstance(requested, str) or requested not in {'feishu', 'qq', 'all'} or (requested != 'all' and set(names) != {requested}):
            raise ValueError('原适配器通道记录冲突，未停止服务')
        return requested
    return names[0] if len(names) == 1 else 'all'


# LLM: 安装切换沿原服务排空；主失败与恢复失败必须同时报告，中断亦尝试原恢复，不将超时/未知签成功。
# 函数用途: 校验并更新原实例，恢复原连接；保留安装首因且不掩盖恢复欠账。
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
        except BaseException as error:
            if services is not None:
                try:
                    update_services(prefix, restore=services)
                except (OSError, ValueError, subprocess.SubprocessError) as restore_error:
                    raise ValueError(f'安装未完成：{error}；服务恢复亦未完成：{restore_error}') from error
            raise
        else:
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



# LLM: 仅本安装进程的tty决定交互阻断，cmdline只补诊断；不向交互进程发信号或输入退出键。
# 函数用途: 更新停服务前要求TUI退出，保留原PID错误前缀供旧调用方识别。
def assert_interactive_processes_exited(prefix):
    for row in inspect_active_processes(prefix):
        if row['interactive']:
            raise ValueError(f"请先退出本安装 TUI/交互进程再更新：PID {row['pid']} ({row['cmdline'] or 'unknown'})")

if __name__ == "__main__":
    raise SystemExit(main())
