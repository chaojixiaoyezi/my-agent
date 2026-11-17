"""阻止已核出生身份的运行服务被直接停止；仅覆盖明确支持的命令语法。

覆盖：任一命令段里的 kill（字面进程号、进程组、-1、执行时展开的目标）与
my-agent / python -m agent_py_agent 的 gateway|adapter|supervisor stop|restart。不覆盖 pkill/killall
等按名字匹配的入口，也不展开 sh -c 字符串。
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..gateway_parts.daemon_metadata import process_identity_is_live

_SERVICES = ('gateway', 'adapter', 'supervisor')
_CLI_NAMES = frozenset({'my-agent', 'my_agent', 'agent_py_agent'})
_FALLBACK_TOKEN_RE = re.compile(r'[();<>|&]+|[^\s();<>|&]+')
_UNRESOLVED_NEXT_STEP = ('未发送信号；目标进程号要到执行时才能确定，无法先核对它是不是运行中的服务。'
    '请先查出具体进程号再用字面进程号重试；运行中的服务请用 restart_gateway 安排安全重启。')


# LLM: W6 live（2026-11-17）：`kill -TERM <gateway pid>; echo "kill_exit=$?"` 直接停掉了服务——旧实现只看整条
#   命令的第一个词，`;` 后的 `85591;` 也不是纯数字。现按 shell 控制符（; && || | & 括号）切段、跳过重定向，
#   每段里任何位置的 kill / my-agent 都检查；进程组（`-PGID`）、`-1`（全部进程）、执行时才展开的目标
#   （`$(...)`、`$VAR`、反引号、xargs 供给）只要有已核出生的服务在运行就拒绝，因为执行前无法证明安全。
#   pkill/killall 这类按名字匹配的入口不在覆盖范围（见模块说明）。
# 函数用途: 把命令切成按控制符分隔的词段，重定向符及其目标不计入词段。
def _command_segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        # 引号不配对（如 heredoc 正文里的 don't）时 shell 照样执行；退回不认引号的切词，宁可多查。
        tokens = _FALLBACK_TOKEN_RE.findall(command)
    segments: list[list[str]] = [[]]
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
        elif token and set(token) <= set('();<>|&') and set(token) & set('<>'):
            skip_next = True
        elif token and set(token) <= set('();|&'):
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


# 函数用途: 解析一次 kill 调用的参数：字面进程号、进程组号，以及是否有执行时才能确定的目标。
def _kill_targets(args: list[str]) -> tuple[set[int], set[int], bool]:
    if args[:1] in (['-l'], ['-L']):
        return set(), set(), False
    index = 2 if args[:1] in (['-s'], ['-n']) else 1 if args[:1] and args[0].startswith('-') and args[0] != '--' else 0
    if args[index:index + 1] == ['--']:
        index += 1
    pids: set[int] = set()
    groups: set[int] = set()
    unresolved = False
    for word in args[index:]:
        if word.isdecimal():
            pids.add(int(word))
        elif word[:1] == '-' and word[1:].isdecimal():
            groups.add(int(word[1:]))
        elif '$' in word or '`' in word:
            unresolved = True
    return pids, groups, unresolved


# 函数用途: 读取进程所在的进程组；拿不到（进程已退出或平台不支持）时返回 None。
def _process_group(pid: object) -> int | None:
    try:
        return os.getpgid(pid) if isinstance(pid, int) else None
    except (AttributeError, OSError):
        return None


# LLM: terminal_session 的 write 把 data 敲进交互 shell，回车后同样会执行；只看 start 的 command 会让
#   `write data="kill <服务进程号>"` 绕过本闸门。
# 函数用途: 取出一次 shell 类工具调用将要执行的命令文本（command 与 terminal_session 写入的 data）。
def guarded_command_text(tool_name: str, params: dict) -> str:
    parts = [str(params.get('command') or '')]
    if tool_name == 'terminal_session':
        parts.append(str(params.get('data') or ''))
    return '\n'.join(part for part in parts if part)


def active_service_stop_denial(command: str, *, data_root: Path,
        foreground_admin_full_access: bool = False, service_root: Path | None = None) -> dict[str, object] | None:
    if foreground_admin_full_access is not True:
        return None
    segments = _command_segments(command)
    if not segments:
        return None
    targets: set[int] = set()
    groups: set[int] = set()
    unresolved = False
    services: set[str] = set()
    for words in segments:
        for index, word in enumerate(words):
            name = word.rsplit('/', 1)[-1]
            if name == 'kill':
                pids, pgids, late = _kill_targets(words[index + 1:])
                if not pids and not pgids and any(prior.rsplit('/', 1)[-1] == 'xargs' for prior in words[:index]):
                    late = True
                targets |= pids
                groups |= pgids
                unresolved = unresolved or late
            elif name in _CLI_NAMES:
                rest = words[index + 1:]
                services.update(role for role, action in zip(rest, rest[1:])
                                if role in _SERVICES and action in {'stop', 'restart'})
    everything = 1 in groups
    if not targets and not groups and not unresolved and not services:
        return None
    root = service_root if service_root is not None else Path(data_root) / 'owners' / 'local' / 'main' / 'workspace' / 'runtime' / 'services' / 'gateway'
    for role in _SERVICES:
        try:
            raw = read_bytes_beneath(root, (f'{role}.pid',), max_bytes=65536, require_dir_fd=True)
            record = json.loads(raw)
        except (OSError, ValueError, TypeError):
            continue
        if (not isinstance(record, dict) or record.get('kind') not in ({'my-agent-adapter', 'my-agent-gateway'} if role == 'adapter' else {f'my-agent-{role}'})
                or process_identity_is_live(record) is not True):
            continue
        pid = record.get('pid')
        if (role in services or everything or unresolved or pid in targets
                or (groups and _process_group(pid) in groups)):
            next_step = (_UNRESOLVED_NEXT_STEP if unresolved and role not in services and pid not in targets else
                '未发送信号；请用 restart_gateway 安排安全重启，或 runtime_governance 核对后经原管理入口操作。')
            return {'error_code': 'ACTIVE_SERVICE_SELF_STOP_BLOCKED', 'effect_outcome': 'not_started',
                'role': role, 'next_step': next_step}
    return None
