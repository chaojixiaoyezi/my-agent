# LLM: 仅管理显式prefix；清理限同uid、无tty和已核出生身份，Linux x86_64可走pidfd系统调用，绝不裸PID发信号。
# 模块用途: 离线校验和原子升级，先暂停守望与排空服务，再精准清理本安装遗留脚本，最后恢复原服务。
from __future__ import annotations

import argparse
import ctypes
import errno
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
import tempfile
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


# LLM: 只消费调用方已打开且核验身份的pidfd；优先Python接口，仅Linux x86_64/amd64允许424，不使用裸PID。
# 函数用途: 给安全进程句柄发送信号，兼容便携Python缺少接口，并保留内核退出及权限错误。
def _send_pidfd_signal(descriptor: int, sig: int) -> None:
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal(descriptor, sig)
        return
    if sys.platform != 'linux' or os.uname().machine not in ('x86_64', 'amd64'):
        raise ValueError('缺少安全pidfd信号接口，请手动停止后重试')
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.restype = ctypes.c_long
    if syscall(424, int(descriptor), int(sig), None, 0) < 0:
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            raise ProcessLookupError(error, os.strerror(error))
        raise OSError(error, os.strerror(error))


# LLM: 优先os.pidfd_open，便携Python缺接口时仅Linux x86_64/amd64经434号syscall打开句柄；ESRCH但目标身份仍在时按缺安全句柄拒绝。
# 函数用途: 打开指定PID的安全内核句柄，进程已消失抛ProcessLookupError，平台或内核不支持抛ValueError。
def _open_pidfd(pid: int) -> int:
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    if sys.platform != 'linux' or os.uname().machine not in ('x86_64', 'amd64'):
        raise ValueError(f'缺少安全pidfd进程句柄，未自动终止 PID {pid}，请手动停止后重试')
    syscall = ctypes.CDLL(None, use_errno=True).syscall
    syscall.restype = ctypes.c_long
    descriptor = int(syscall(434, int(pid), 0))
    if descriptor < 0:
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            if _process_identity(pid) is not None:
                raise ValueError(f'缺少安全pidfd进程句柄，未自动终止 PID {pid}，请手动停止后重试')
            raise ProcessLookupError(error, os.strerror(error))
        if error in (errno.ENOSYS, errno.EPERM, errno.EINVAL):
            raise ValueError(f'缺少安全pidfd进程句柄，未自动终止 PID {pid}，请手动停止后重试')
        raise OSError(error, os.strerror(error))
    return descriptor


# LLM: 必须拿到pidfd且打开前后身份一致才发送；句柄与发送器均可走受限syscall，finally关FD，绝不裸PID发信号。
# 函数用途: 给原一代进程发TERM或KILL，退出/换代无信号，缺安全句柄时拒绝自动清理。
def _signal_process(pid, identity, sig):
    if _process_identity(pid) != identity:
        return
    try:
        descriptor = _open_pidfd(pid)
        try:
            if _process_identity(pid) == identity:
                _send_pidfd_signal(descriptor, sig)
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
#   默认注入 glibc 低碎片 Arena/Trim 阈值与 macOS MallocLargeCache=0，防止多线程大 JSON 解析后常驻内存虚高。
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
export MALLOC_ARENA_MAX="${{MALLOC_ARENA_MAX:-2}}"
export MALLOC_TRIM_THRESHOLD_="${{MALLOC_TRIM_THRESHOLD_:-131072}}"
export MallocLargeCache="${{MallocLargeCache:-0}}"
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


# 已知旧出厂值才能迁移；其它自定义值保持不动。
_UPGRADEABLE_DEFAULTS = (
    ("gateway_http_max_request_workers", ("16", "64", "128"), "32"),
    ("gateway_http_max_outstanding_requests", ("128",), "256"),
    ("gateway_user_inflight_limit", ("50",), "128"),
    ("owner_agent_pool_max_agents", ("64",), "128"),
    ("background_admin_threads_per_owner", ("16",), "64"),
    ("background_threads_per_owner", ("4",), "2"),
    ("background_owner_workers", ("8", "32"), "128"),
)
# 缺失键的值及中文说明取自发行模板，不复制一套出厂默认。
_NEW_CONCURRENCY_KEYS = (
    "gateway_http_control_lane_workers",
    "gateway_native_stream_max",
    "background_admin_threads_per_owner",
)
_RETIRED_KEYS = ("gateway_worker_process_count",)


# LLM: 只取模板中顶层键自己的注释块与键行，不解析或重排其它内容；不存在时交调用方告警。
# 函数用途: 从发行模板抽取缺失并发键及中文说明。
def _template_concurrency_block(template_content: str, key: str) -> list[str]:
    lines = template_content.splitlines()
    pattern = re.compile(rf"^{re.escape(key)}[ \t]*:")
    for index in range(len(lines) - 1, -1, -1):
        if pattern.match(lines[index]):
            start = index
            while start > 0 and lines[start - 1].startswith("#"):
                start -= 1
            return lines[start:index + 1]
    return []


# LLM: 同目录临时文件+fsync+os.replace，权限固定0600；异常清理临时文件且原件不动。
# 函数用途: 原子写回升级配置，避免中断留下半写文件或放宽配置权限。
def _atomic_write_config(config_path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".agent_config.upgrade-", suffix=".tmp", dir=str(config_path.parent))
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
        replaced = True
    finally:
        if not replaced:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


# LLM: 只删退役零值及紧邻顶层注释；缩进#行可能是用户块标量内容，不能作为注释删除。
# 函数用途: 移除旧的gateway_worker_process_count零值配置。
def _remove_retired_keys(lines: list[str]) -> list[str]:
    remaining: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        for key in _RETIRED_KEYS:
            match = re.fullmatch(rf"{re.escape(key)}[ \t]*:[ \t]*(?P<value>[^\r\n#]*)(?:[ \t]*#.*)?", body)
            if match is not None and match.group("value").strip().strip("\"'") == "0":
                while remaining and remaining[-1].startswith("#"):
                    remaining.pop()
                break
        else:
            remaining.append(line)
    return remaining


# LLM: 只迁移列明旧出厂值、删除退役零值、补齐新键；其它设置、注释与行序不变。
#   重复升级无变化不写盘；非UTF-8明确告警并保留原件；失败向上抛且不破坏旧配置。
# 函数用途: 让已有机器在initialize_data升级入口获得本发行版并发默认，不覆盖用户配置。
def upgrade_existing_config_defaults(config_path: Path, template_content: str) -> None:
    try:
        original = config_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        print(f"跳过并发配置迁移：{config_path} 不是 UTF-8 文本，原文件保持不变")
        return
    lines = original.splitlines(keepends=True)
    for key, old_values, new_value in _UPGRADEABLE_DEFAULTS:
        pattern = re.compile(rf"^(?P<prefix>{re.escape(key)}[ \t]*:[ \t]*)(?P<rest>[^\r\n]*)$")
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            match = pattern.fullmatch(body)
            if match is None:
                continue
            value = match.group("rest").partition("#")[0]
            if value.strip().strip("\"'") not in old_values:
                continue
            # 只替换标量，后面的空白、行内注释与行尾按原样接回。
            tail = match.group("rest")[len(value.rstrip()):]
            lines[index] = match.group("prefix") + new_value + tail + line[len(body):]
    lines = _remove_retired_keys(lines)
    text = "".join(lines)
    appended = False
    for key in _NEW_CONCURRENCY_KEYS:
        if re.search(rf"^{re.escape(key)}[ \t]*:", text, re.M):
            continue
        block = _template_concurrency_block(template_content, key)
        if not block:
            print(f"跳过补齐并发配置项：发行包模板中没有 {key}")
            continue
        if not appended:
            if not text.endswith(("\n", "\r")):
                text += "\n"
            text += "\n# 以下并发配置项由安装升级自动补齐，值取自本发行版模板；修改后重启 Gateway 生效。\n"
            appended = True
        text += "\n".join(block) + "\n"
    if text != original:
        _atomic_write_config(config_path, text)


_FEISHU_PROGRESS_MIGRATION = 'feishu-progress-default-v1'


def _migrate_config_once(prefix: Path, release: Path, config: Path, migration: str, transform) -> None:
    """在原安装锁内一次采用明确的新默认；意图/完成均记原安装metadata，重试不重开用户false。"""
    marker = prefix / MARKER
    if not marker.exists():
        return
    record = json.loads(marker.read_text(encoding='utf-8'))
    migrations = record.get('config_migrations', {})
    if not isinstance(migrations, dict):
        raise ValueError('安装配置迁移记录损坏')
    previous = migrations.get(migration)
    if isinstance(previous, dict) and previous.get('phase') == 'completed':
        return
    original = config.read_text(encoding='utf-8')
    before_hash = hashlib.sha256(original.encode('utf-8')).hexdigest()
    if previous is not None:
        if not isinstance(previous, dict) or previous.get('phase') != 'prepared':
            raise ValueError('飞书过程卡迁移状态不可确认')
        if before_hash == previous.get('after_sha256'):
            completed = {**previous, 'phase': 'completed', 'completed_at': time.time()}
            _atomic_write_config(marker, json.dumps({**record, 'config_migrations': {**migrations,
                migration: completed}}, ensure_ascii=False, indent=2) + '\n')
            return
        if before_hash != previous.get('before_sha256'):
            raise ValueError('飞书过程卡迁移期间配置已改变，保留原配置')
    updated, old, new = transform(original)
    prepared = {'phase': 'prepared', 'release': release.name, 'old_value': old,
        'new_value': new, 'before_sha256': before_hash,
        'after_sha256': hashlib.sha256(updated.encode('utf-8')).hexdigest(), 'prepared_at': time.time()}
    _atomic_write_config(marker, json.dumps({**record, 'config_migrations': {**migrations,
        migration: prepared}}, ensure_ascii=False, indent=2) + '\n')
    if updated != original:
        _atomic_write_config(config, updated)
    completed = {**prepared, 'phase': 'completed', 'completed_at': time.time()}
    _atomic_write_config(marker, json.dumps({**record, 'config_migrations': {**migrations,
        migration: completed}}, ensure_ascii=False, indent=2) + '\n')


def migrate_feishu_progress_default(prefix: Path, release: Path, config: Path, template: str) -> None:
    if not re.search(r'^im_progress_cards_enabled:\s*true\s*(?:#.*)?$', template, re.M):
        return
    _migrate_config_once(prefix, release, config, _FEISHU_PROGRESS_MIGRATION, _progress_default_text)


def _progress_default_text(original: str):
    pattern = re.compile(r'^(?P<prefix>im_progress_cards_enabled[ \t]*:[ \t]*)(?P<value>[^#\r\n]*)(?P<tail>[^\r\n]*)$', re.M)
    matches = list(pattern.finditer(original))
    if len(matches) > 1:
        raise ValueError('飞书过程卡配置重复，未迁移')
    old = matches[0].group('value').strip().strip('"\'').lower() if matches else None
    if old not in (None, 'false', 'true'):
        raise ValueError('飞书过程卡配置不是布尔值，未迁移')
    if old == 'false':
        match = matches[0]
        value = match.group('value')
        replacement = match.group('prefix') + 'true' + value[len(value.rstrip()):] + match.group('tail')
        updated = original[:match.start()] + replacement + original[match.end():]
    elif old is None:
        updated = original.rstrip('\n') + '\n\n# 本次更新采用飞书过程卡默认；/verbose off可关闭当前会话。\nim_progress_cards_enabled: true\n'
    else:
        updated = original
    return updated, None if old is None else old == 'true', True


def migrate_feishu_oauth_defaults(prefix: Path, release: Path, package: Path) -> None:
    """仅替换旧模板的准确scope列表；不改授权/token，定制列表与空列表原样保留。"""
    from agent_py_agent.agent.settings.config_io import _strip_yaml_comment, parse_simple_yaml_text

    def parse_config(text):
        result = parse_simple_yaml_text(text)
        value = result.get('feishu_oauth_scopes')
        # 正式简化loader仅认Python式引号列表；旧YAML裸scope只在本次迁移
        # 按OAuth标识符的无转义语法核验，绝不将其它配置或复杂YAML重新解释。
        if isinstance(value, str) and value.startswith('[') and value.endswith(']'):
            items = value[1:-1].split(',')
            scopes = []
            for item in items:
                item = item.strip()
                if len(item) >= 2 and item[0] == item[-1] and item[0] in '\"\'':
                    item = item[1:-1]
                if not re.fullmatch(r'[A-Za-z0-9_:./-]+', item):
                    return result
                scopes.append(item)
            result['feishu_oauth_scopes'] = scopes
        return result

    config = prefix / 'data/config/capability_config.yaml'
    template = package / 'config/capability_config.yaml'
    if not config.exists() or not template.exists():
        return
    defaults = parse_config(template.read_text(encoding='utf-8')).get('feishu_oauth_scopes')
    if not isinstance(defaults, list) or defaults == ['offline_access']:
        return

    def transform(original):
        parsed = parse_config(original)
        old = parsed.get('feishu_oauth_scopes')
        if old != ['offline_access']:
            return original, old, old
        # 与正式配置读取共用标准库语法；只改准确旧默认，保持其它行逐字不动。
        lines = original.splitlines(keepends=True)
        matches = [index for index, line in enumerate(lines)
                   if re.match(r'^feishu_oauth_scopes[ \t]*:', line)]
        if len(matches) != 1:
            raise ValueError('飞书授权scope配置重复或不可确认')
        index = matches[0]
        raw = lines[index]
        code = _strip_yaml_comment(raw).rstrip()
        colon = code.index(':') + 1
        if not code[colon:].strip():
            items = []
            for cursor in range(index + 1, len(lines)):
                item_code = _strip_yaml_comment(lines[cursor]).rstrip()
                if not item_code.strip():
                    continue
                if not item_code.lstrip().startswith('- '):
                    break
                items.append((cursor, item_code))
            if len(items) != 1:
                raise ValueError('飞书授权scope列表不能独立迁移')
            cursor, item_code = items[0]
            # 原列表项退为注释/空行；保留其行尾注释和相邻字段注释。
            suffix = lines[cursor][len(item_code):]
            lines[cursor] = suffix.lstrip(' \t')
        lines[index] = raw[:colon] + ' ' + json.dumps(defaults, ensure_ascii=False) + raw[len(code):]
        updated = ''.join(lines)
        validated = parse_config(updated)
        if (validated.get('feishu_oauth_scopes') != defaults
                or {key: value for key, value in validated.items() if key != 'feishu_oauth_scopes'}
                != {key: value for key, value in parsed.items() if key != 'feishu_oauth_scopes'}):
            raise ValueError('飞书授权scope迁移改变了其它配置，未写入')
        return updated, old, defaults

    _migrate_config_once(prefix, release, config, 'feishu-oauth-default-scopes-v1', transform)


def migrate_feishu_history_default(prefix: Path, release: Path, package: Path) -> None:
    """旧模板的历史查询开关一次采用本次用户要求的新默认，后续显式关闭保持。"""
    config = prefix / 'data/config/capability_config.yaml'
    template = package / 'config/capability_config.yaml'
    if not config.exists() or not template.exists():
        return
    if not re.search(r'^feishu_chat_history_enabled:\s*true\s*(?:#.*)?$', template.read_text(encoding='utf-8'), re.M):
        return

    def transform(original):
        pattern = re.compile(r'^(?P<prefix>feishu_chat_history_enabled[ \t]*:[ \t]*)(?P<value>[^#\r\n]*)(?P<tail>[^\r\n]*)$', re.M)
        matches = list(pattern.finditer(original))
        if len(matches) > 1:
            raise ValueError('飞书历史查询开关重复，未迁移')
        old = matches[0].group('value').strip().strip('"\'').lower() if matches else None
        if old not in (None, 'false', 'true'):
            raise ValueError('飞书历史查询开关不是布尔值，未迁移')
        if old == 'false':
            match = matches[0]
            value = match.group('value')
            updated = (original[:match.start()] + match.group('prefix') + 'true'
                       + value[len(value.rstrip()):] + match.group('tail') + original[match.end():])
        elif old is None:
            updated = original.rstrip('\n') + '\n\n# 更新后默认提供飞书历史查询；实际读取仍核验本人/群权限。\nfeishu_chat_history_enabled: true\n'
        else:
            updated = original
        return updated, old, True

    _migrate_config_once(prefix, release, config, 'feishu-chat-history-default-v1', transform)


# LLM: 首装保留原owner目录权威；模板优先随包3.14/3.14t并兼容旧目录，更新不覆盖用户模型、凭据或路径。
# 函数用途: 初始化空白数据，已有配置通过受限的升级迁移入口更新。
def initialize_data(prefix: Path, release: Path) -> None:
    config = prefix / "data" / "config" / "agent_config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (prefix / "data/tmp").mkdir(exist_ok=True, mode=0o700)
    (prefix / "data/cache").mkdir(exist_ok=True, mode=0o700)
    workspace_path(prefix).mkdir(parents=True, exist_ok=True)
    lib = release / "runtime/lib"
    preferred = [lib / name / "site-packages/agent_py_agent" for name in ("python3.14", "python3.14t")]
    candidates = [path for path in preferred if path.is_dir()]
    if not candidates:
        candidates = sorted((path for path in lib.glob("python3.*/site-packages/agent_py_agent") if path.is_dir()), reverse=True)
    package = candidates[0] if candidates else lib / "python3.11/site-packages/agent_py_agent"
    content = (package / "config" / "agent_config.yaml").read_text(encoding="utf-8")
    migrate_feishu_oauth_defaults(prefix, release, package)
    migrate_feishu_history_default(prefix, release, package)
    if config.exists():
        upgrade_existing_config_defaults(config, content)
        migrate_feishu_progress_default(prefix, release, config, content)
        return
    # 空工作区配置保留原 owner-home 权威；环境绑定 data 后无需另建外部目录或放宽权限。
    content = re.sub(r'^gateway_port:.*$', 'gateway_port: 18420', content, flags=re.M)
    with config.open("x", encoding="utf-8") as stream:
        stream.write(content)
    config.chmod(0o600)
    migrate_feishu_progress_default(prefix, release, config, content)


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
        if marker.exists():
            existing = json.loads(marker.read_text(encoding="utf-8"))
            if ({key: value for key, value in existing.items() if key != 'config_migrations'} != record
                    or not isinstance(existing.get('config_migrations', {}), dict)):
                raise ValueError("安装记录与目标路径不一致，请不要直接搬移运行中的安装目录")
            record = existing
        _atomic_write_config(marker, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
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
        record = json.loads(marker.read_text(encoding="utf-8"))
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


# LLM: 原gateway/adapter控制失败仍硬拒绝；旧发行adapter stop仅等5秒时补等最多15秒确认进程已退；supervisor-stop允许旧CLI无此命令。
# 函数用途: 串行执行准确安装启动器的服务动作，不使用shell或按名字杀进程。
def _run_update_commands(prefix, commands):
    launcher = str(prefix / 'bin' / 'my-agent-preview')
    for arguments in commands:
        result = subprocess.run([launcher, *arguments], timeout=6000, check=False)
        if result.returncode and arguments[:2] == ['adapter', 'stop'] and _wait_legacy_adapter_exited(prefix):
            continue
        if result.returncode and arguments[:2] != ['gateway', 'supervisor-stop']:
            raise ValueError('原实例服务控制未完成：' + ' '.join(arguments[:2]))
        if result.returncode:
            print('Supervisor停止命令未成功，继续原服务排空与遗留进程核验（旧发行可能不支持该命令）')


# LLM: 旧发行adapter stop发SIGTERM后仅等5秒，此处补等最多15秒，仅在原adapter_pid确实退出后清理遗留文件并放行。
# 函数用途: 兼容旧版CLI停止超时过短导致的飞书通道收尾假失败。
def _wait_legacy_adapter_exited(prefix: Path, timeout: float = 15.0) -> bool:
    paths = _update_service_paths(prefix)
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if not _service_alive(paths.adapter_pid):
            paths.adapter_pid.unlink(missing_ok=True)
            (paths.root / 'adapter_stop.request').unlink(missing_ok=True)
            return True
        time.sleep(0.2)
    return not _service_alive(paths.adapter_pid)


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
    if not names or len(names) != len(rows) or any(not isinstance(name, str) or name not in {'feishu'} for name in names) or len(names) != len(set(names)):
        raise ValueError('原适配器通道记录不可用，未停止服务')
    requested = state.get('requested_channel')
    if requested is not None:
        if not isinstance(requested, str) or requested not in {'feishu', 'all'} or (requested != 'all' and set(names) != {requested}):
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
