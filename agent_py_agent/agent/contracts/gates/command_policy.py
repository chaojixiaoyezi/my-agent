# LLM: Shared shell command policy parses structured command inputs before any gateway-specific allowlist.
# 模块用途: 为主代理 gate 和子代理 shell_gateway 提供同一套危险命令/参数/操作符机器码判断。

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CATASTROPHIC_EXECUTABLES = frozenset({"shutdown", "reboot", "halt", "poweroff", "telinit"})
_COMMAND_WRAPPERS = frozenset({"sudo", "command", "exec", "builtin", "nohup", "time"})
_PROTECTED_DELETE_PREFIXES = (
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/lib64",
    "/private/etc",
    "/private/var",
    "/root",
    "/sbin",
    "/sys",
    "/System",
    "/usr",
    "/var",
)
_SHELL_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_DOWNLOAD_EXECUTABLES = frozenset({"curl", "wget"})
_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:")


# LLM: CommandPolicyFinding is the shared machine-readable shell policy finding.
# 类用途: 保存稳定错误码和结构化证据，避免调用方解析自然语言错误文本。
@dataclass(frozen=True)
class CommandPolicyFinding:
    code: str
    evidence: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "evidence": dict(self.evidence)}


# LLM: CommandPolicyDecision carries both parsed argv and deny findings.
# 类用途: 让调用方复用同一次结构化解析结果，避免主/子代理各写一套 shell 表。
@dataclass(frozen=True)
class CommandPolicyDecision:
    argv: tuple[str, ...] = ()
    findings: tuple[CommandPolicyFinding, ...] = ()

    # LLM: allowed keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    @property
    def allowed(self) -> bool:
        return not self.findings

    # LLM: finding_codes keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    @property
    def finding_codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


# LLM: evaluate_command_policy applies fail-closed shell command checks to string or argv inputs.
# 函数用途: 结构化解析 command/argv，按危险模式、危险 executable、shell 操作符顺序输出机器码。
def evaluate_command_policy(
    command: object,
    *,
    allow_shell_operators: bool = False,
    allowed_commands: Iterable[str] = (),
) -> CommandPolicyDecision:
    parsed = _parse_command_value(command)
    if parsed.findings:
        return parsed
    findings, covered_positions = _dangerous_pattern_findings(parsed.argv)
    if not allow_shell_operators:
        findings.extend(_shell_operator_findings(parsed.argv, _raw_command_text(command)))
    findings.extend(_dangerous_executable_findings(parsed.argv, covered_positions, frozenset(allowed_commands)))
    return CommandPolicyDecision(parsed.argv, _unique_findings(findings))


# LLM: command_name normalizes executable names for policy and gateway allowlists.
# 函数用途: 提取 basename 并小写，覆盖 /bin/rm 和 mkfs.ext4 这类可执行路径。
def command_name(value: object) -> str:
    return Path(str(value or "")).name.lower()


# LLM: _parse_command_value keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _parse_command_value(command: object) -> CommandPolicyDecision:
    if isinstance(command, list):
        return _parse_argv_value(command)
    text = str(command or "").strip()
    if not text:
        return CommandPolicyDecision(findings=(CommandPolicyFinding("COMMAND_EMPTY"),))
    if "\x00" in text:
        return CommandPolicyDecision(findings=(CommandPolicyFinding("COMMAND_ARGV_INVALID"),))
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = tuple(lexer)
    except ValueError as exc:
        return CommandPolicyDecision(
            findings=(CommandPolicyFinding("COMMAND_PARSE_FAILED", {"error": str(exc)}),)
        )
    return CommandPolicyDecision(argv or (), () if argv else (CommandPolicyFinding("COMMAND_EMPTY"),))


# LLM: _parse_argv_value keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _parse_argv_value(value: list[object]) -> CommandPolicyDecision:
    argv: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or "\x00" in text:
            return CommandPolicyDecision(findings=(CommandPolicyFinding("COMMAND_ARGV_INVALID"),))
        argv.append(text)
    return CommandPolicyDecision(tuple(argv), () if argv else (CommandPolicyFinding("COMMAND_EMPTY"),))


# LLM: _dangerous_pattern_findings only blocks catastrophic command shapes; normal cleanup remains controlled by workspace policy.
# 函数用途: 识别根目录/系统目录删除、裸下载执行、fork bomb 和裸盘写入，不把普通 rm/chmod 当成硬失败。
def _dangerous_pattern_findings(argv: tuple[str, ...]) -> tuple[list[CommandPolicyFinding], set[int]]:
    findings: list[CommandPolicyFinding] = []
    covered_positions: set[int] = set()
    raw = " ".join(argv)
    if _FORK_BOMB_RE.search(raw):
        findings.append(CommandPolicyFinding("COMMAND_DANGEROUS_PATTERN_BLOCKED", {"pattern": "FORK_BOMB"}))
    command_positions = _effective_command_positions(argv)
    for position in command_positions:
        executable = command_name(argv[position])
        args = _command_args(argv, position)
        if executable == "rm" and _rm_deletes_protected_target(args):
            covered_positions.add(position)
            findings.append(
                CommandPolicyFinding(
                    "COMMAND_DANGEROUS_PATTERN_BLOCKED",
                    {"pattern": "RM_PROTECTED_TARGET", "executable": executable},
                )
            )
        if executable == "dd" and _dd_writes_raw_device(args):
            covered_positions.add(position)
            findings.append(
                CommandPolicyFinding(
                    "COMMAND_DANGEROUS_PATTERN_BLOCKED",
                    {"pattern": "DD_RAW_DEVICE_WRITE", "executable": executable},
                )
            )
        if executable == "init" and args and args[0] in {"0", "6"}:
            covered_positions.add(position)
            findings.append(
                CommandPolicyFinding(
                    "COMMAND_DANGEROUS_PATTERN_BLOCKED",
                    {"pattern": "INIT_SHUTDOWN_REBOOT", "executable": executable},
                )
            )
    if _download_piped_to_shell(argv, command_positions):
        findings.append(
            CommandPolicyFinding("COMMAND_DANGEROUS_PATTERN_BLOCKED", {"pattern": "DOWNLOAD_PIPE_TO_SHELL"})
        )
    return findings, covered_positions


# LLM: _shell_operator_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _shell_operator_findings(argv: tuple[str, ...], raw: str) -> list[CommandPolicyFinding]:
    operators = {token for token in argv if _is_shell_operator_token(token)}
    if "`" in raw:
        operators.add("`")
    if "$(" in raw:
        operators.add("$(")
    if "\n" in raw or "\r" in raw:
        operators.add("newline")
    if not operators:
        return []
    return [CommandPolicyFinding("COMMAND_SHELL_OPERATOR_BLOCKED", {"operators": sorted(operators)})]


# LLM: _dangerous_executable_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _dangerous_executable_findings(
    argv: tuple[str, ...],
    covered_positions: set[int],
    allowed_commands: frozenset[str] = frozenset(),
) -> list[CommandPolicyFinding]:
    findings: list[CommandPolicyFinding] = []
    for position in _effective_command_positions(argv):
        if position in covered_positions:
            continue
        executable = command_name(argv[position])
        if _is_dangerous_executable(executable):
            findings.append(
                CommandPolicyFinding("COMMAND_DANGEROUS_EXECUTABLE_BLOCKED", {"executable": executable})
            )
    return findings


# LLM: _command_positions keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    expect_command = True
    for index, token in enumerate(argv):
        if _is_shell_operator_token(token):
            expect_command = True
            continue
        if expect_command and _looks_like_assignment(token):
            continue
        if expect_command:
            positions.append(index)
            expect_command = False
    return positions


# LLM: _effective_command_positions unwraps sudo/env/time-style launchers before checking the real command.
# 函数用途: 防止 sudo rm、env VAR=x rm、timeout 5 rm 这类包装绕过灾难命令判断。
def _effective_command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    for position in _command_positions(argv):
        effective = _unwrap_command_position(argv, position)
        if effective not in positions:
            positions.append(effective)
    return positions


# LLM: _unwrap_command_position keeps policy target-aware while allowing harmless wrappers.
# 函数用途: 跳过 sudo/env/time/timeout/nice/nohup 等启动包装，返回实际可执行文件位置。
def _unwrap_command_position(argv: tuple[str, ...], position: int) -> int:
    current = position
    while current < len(argv):
        executable = command_name(argv[current])
        if executable in _COMMAND_WRAPPERS:
            current += 1
            if executable == "sudo":
                while current < len(argv) and argv[current].startswith("-"):
                    current += 1
            continue
        if executable == "env":
            current += 1
            while current < len(argv) and (argv[current].startswith("-") or _looks_like_assignment(argv[current])):
                current += 1
            continue
        if executable == "timeout":
            current += 1
            while current < len(argv) and argv[current].startswith("-"):
                current += 1
            if current < len(argv):
                current += 1
            continue
        if executable == "nice":
            current += 1
            if current < len(argv) and argv[current] == "-n":
                current += 2
            elif current < len(argv) and argv[current].startswith("-"):
                current += 1
            continue
        return current
    return position


# LLM: _command_args keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _command_args(argv: tuple[str, ...], position: int) -> list[str]:
    args: list[str] = []
    for token in argv[position + 1 :]:
        if _is_shell_operator_token(token):
            break
        args.append(token)
    return args


# LLM: _rm_deletes_protected_target blocks only last-resort deletes, not ordinary workspace cleanup.
# 函数用途: 只拒绝 /、系统目录、家目录变量等灾难目标；rm file 和 rm -rf build 交给工作区策略控制。
def _rm_deletes_protected_target(args: list[str]) -> bool:
    return any(_is_protected_delete_target(arg) for arg in args if not _is_rm_option(arg))


# LLM: _is_rm_option distinguishes rm flags from targets without interpreting prose.
# 函数用途: 过滤 -rf、--force、-- 等参数，避免把选项当路径。
def _is_rm_option(arg: str) -> bool:
    return arg == "--" or (arg.startswith("-") and arg != "-")


# LLM: _is_protected_delete_target recognizes shell-expanded and literal protected roots.
# 函数用途: 拦截 /、/etc、$HOME、~ 等高损害删除目标，同时允许 /tmp/build 这类普通清理。
def _is_protected_delete_target(arg: str) -> bool:
    raw = arg.strip()
    if raw in {"/", "/*"}:
        return True
    target = raw.rstrip("/")
    if not target:
        return False
    if target in {"~", "$HOME", "${HOME}", "$USERPROFILE", "${USERPROFILE}"}:
        return True
    if target.startswith(("~/", "$HOME/", "${HOME}/", "$USERPROFILE/", "${USERPROFILE}/")):
        return True
    target = target.rstrip("*").rstrip("/")
    if not target:
        target = "/"
    for prefix in _PROTECTED_DELETE_PREFIXES:
        normalized_prefix = prefix.rstrip("/") or "/"
        if target == normalized_prefix or target.startswith(normalized_prefix + "/"):
            return True
    return False


# LLM: _dd_writes_raw_device keeps dd usable for files while blocking disk clobbering.
# 函数用途: 只在 of= 指向裸设备时拒绝 dd；生成本地镜像文件不被当成系统破坏。
def _dd_writes_raw_device(args: list[str]) -> bool:
    for arg in args:
        if not arg.startswith("of="):
            continue
        target = arg.partition("=")[2]
        if _is_raw_device_path(target):
            return True
    return False


# LLM: _is_raw_device_path checks common Linux/macOS block or raw disk targets.
# 函数用途: 识别 /dev/sda、/dev/disk0、/dev/rdisk0、/dev/nvme0n1 等磁盘目标。
def _is_raw_device_path(value: str) -> bool:
    return bool(
        re.match(
            r"^/dev/(sd[a-z]\d*|hd[a-z]\d*|vd[a-z]\d*|xvd[a-z]\d*|nvme\d+n\d+(p\d+)?|"
            r"mmcblk\d+(p\d+)?|disk\d+s?\d*|rdisk\d+s?\d*)$",
            value,
            re.IGNORECASE,
        )
    )


# LLM: _has_short_flag checks compact shell flags without nesting command policy logic.
# 函数用途: 识别 -rf/-fr 这类短选项组合，避免危险命令判断靠自然语言描述。
def _has_short_flag(arg: str, flag: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and flag in arg.lstrip("-")


# LLM: _download_piped_to_shell keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _download_piped_to_shell(argv: tuple[str, ...], command_positions: list[int]) -> bool:
    for left, right in zip(command_positions, command_positions[1:]):
        if "|" not in argv[left + 1 : right]:
            continue
        if command_name(argv[left]) in _DOWNLOAD_EXECUTABLES and command_name(argv[right]) in _SHELL_EXECUTABLES:
            return True
    return False


# LLM: _is_shell_operator_token keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _is_shell_operator_token(token: str) -> bool:
    return bool(token) and set(token).issubset({"&", "|", ";", ">", "<"})


# LLM: _is_dangerous_executable keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _is_dangerous_executable(executable: str) -> bool:
    return executable in _CATASTROPHIC_EXECUTABLES or executable == "mkfs" or executable.startswith("mkfs.")


# LLM: _looks_like_assignment keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _looks_like_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name.replace("_", "").isalnum() and not name[0].isdigit())


# LLM: _raw_command_text keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _raw_command_text(command: object) -> str:
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    return str(command or "")


# LLM: _unique_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _unique_findings(findings: list[CommandPolicyFinding]) -> tuple[CommandPolicyFinding, ...]:
    unique: list[CommandPolicyFinding] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for finding in findings:
        key = (finding.code, tuple(sorted((str(k), str(v)) for k, v in finding.evidence.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return tuple(unique)


__all__ = [
    "CommandPolicyDecision",
    "CommandPolicyFinding",
    "command_name",
    "evaluate_command_policy",
]
