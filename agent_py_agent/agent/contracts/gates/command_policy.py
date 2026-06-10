
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_CATASTROPHIC_EXECUTABLES = frozenset({"shutdown", "reboot", "halt", "poweroff", "telinit"})
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


@dataclass(frozen=True)
class CommandPolicyFinding:
    code: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "evidence": dict(self.evidence)}


@dataclass(frozen=True)
class CommandPolicyDecision:
    argv: tuple[str, ...] = ()
    findings: tuple[CommandPolicyFinding, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.findings

    @property
    def finding_codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings)


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


def _parse_argv_value(value: list[object]) -> CommandPolicyDecision:
    argv: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or "\x00" in text:
            return CommandPolicyDecision(findings=(CommandPolicyFinding("COMMAND_ARGV_INVALID"),))
        argv.append(text)
    return CommandPolicyDecision(tuple(argv), () if argv else (CommandPolicyFinding("COMMAND_EMPTY"),))


def _dangerous_pattern_findings(argv: tuple[str, ...]) -> tuple[list[CommandPolicyFinding], set[int]]:
    findings: list[CommandPolicyFinding] = []
    covered_positions: set[int] = set()
    raw = " ".join(argv)
    if _FORK_BOMB_RE.search(raw):
        findings.append(CommandPolicyFinding("COMMAND_DANGEROUS_PATTERN_BLOCKED", {"pattern": "FORK_BOMB"}))
    command_positions = effective_command_positions(argv)
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


def _shell_operator_findings(argv: tuple[str, ...], raw: str) -> list[CommandPolicyFinding]:
    operators = {token for token in argv if is_shell_operator_token(token)}
    if "`" in raw:
        operators.add("`")
    if "$(" in raw:
        operators.add("$(")
    if "\n" in raw or "\r" in raw:
        operators.add("newline")
    if not operators:
        return []
    return [CommandPolicyFinding("COMMAND_SHELL_OPERATOR_BLOCKED", {"operators": sorted(operators)})]


def _dangerous_executable_findings(
    argv: tuple[str, ...],
    covered_positions: set[int],
    allowed_commands: frozenset[str] = frozenset(),
) -> list[CommandPolicyFinding]:
    findings: list[CommandPolicyFinding] = []
    for position in effective_command_positions(argv):
        if position in covered_positions:
            continue
        executable = command_name(argv[position])
        if _is_dangerous_executable(executable):
            findings.append(
                CommandPolicyFinding("COMMAND_DANGEROUS_EXECUTABLE_BLOCKED", {"executable": executable})
            )
    return findings


def _command_args(argv: tuple[str, ...], position: int) -> list[str]:
    args: list[str] = []
    for token in argv[position + 1 :]:
        if is_shell_operator_token(token):
            break
        args.append(token)
    return args


def _rm_deletes_protected_target(args: list[str]) -> bool:
    return any(_is_protected_delete_target(arg) for arg in args if not _is_rm_option(arg))


def _is_rm_option(arg: str) -> bool:
    return arg == "--" or (arg.startswith("-") and arg != "-")


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


def _dd_writes_raw_device(args: list[str]) -> bool:
    for arg in args:
        if not arg.startswith("of="):
            continue
        target = arg.partition("=")[2]
        if _is_raw_device_path(target):
            return True
    return False


def _is_raw_device_path(value: str) -> bool:
    return bool(
        re.match(
            r"^/dev/(sd[a-z]\d*|hd[a-z]\d*|vd[a-z]\d*|xvd[a-z]\d*|nvme\d+n\d+(p\d+)?|"
            r"mmcblk\d+(p\d+)?|disk\d+s?\d*|rdisk\d+s?\d*)$",
            value,
            re.IGNORECASE,
        )
    )


def _has_short_flag(arg: str, flag: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and flag in arg.lstrip("-")


def _download_piped_to_shell(argv: tuple[str, ...], command_positions: list[int]) -> bool:
    for left, right in zip(command_positions, command_positions[1:]):
        if "|" not in argv[left + 1 : right]:
            continue
        if command_name(argv[left]) in _DOWNLOAD_EXECUTABLES and command_name(argv[right]) in _SHELL_EXECUTABLES:
            return True
    return False


def _is_dangerous_executable(executable: str) -> bool:
    return executable in _CATASTROPHIC_EXECUTABLES or executable == "mkfs" or executable.startswith("mkfs.")


def _raw_command_text(command: object) -> str:
    if isinstance(command, list):
        return " ".join(str(item) for item in command)
    return str(command or "")


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


# ---- 原 command/positions.py 并入 ----
from pathlib import Path

_COMMAND_WRAPPERS = frozenset({"sudo", "command", "exec", "builtin", "nohup", "time"})


def command_name(value: object) -> str:
    return Path(str(value or "")).name.lower()


def effective_command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    for position in command_positions(argv):
        effective = _unwrap_command_position(argv, position)
        if effective not in positions:
            positions.append(effective)
    return positions


def command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    expect_command = True
    for index, token in enumerate(argv):
        if is_shell_operator_token(token):
            expect_command = True
            continue
        if expect_command and looks_like_assignment(token):
            continue
        if expect_command:
            positions.append(index)
            expect_command = False
    return positions


def is_shell_operator_token(token: str) -> bool:
    return bool(token) and set(token).issubset({"&", "|", ";", ">", "<"})


def looks_like_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name.replace("_", "").isalnum() and not name[0].isdigit())


def _unwrap_command_position(argv: tuple[str, ...], position: int) -> int:
    current = position
    while current < len(argv):
        executable = command_name(argv[current])
        if executable in _COMMAND_WRAPPERS:
            current = _skip_simple_wrapper(argv, current, executable)
            continue
        if executable == "env":
            current = _skip_env_wrapper(argv, current)
            continue
        if executable == "timeout":
            current = _skip_timeout_wrapper(argv, current)
            continue
        if executable == "nice":
            current = _skip_nice_wrapper(argv, current)
            continue
        return current
    return position


def _skip_simple_wrapper(argv: tuple[str, ...], current: int, executable: str) -> int:
    current += 1
    if executable == "sudo":
        while current < len(argv) and argv[current].startswith("-"):
            current += 1
    return current


def _skip_env_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    while current < len(argv) and (argv[current].startswith("-") or looks_like_assignment(argv[current])):
        current += 1
    return current


def _skip_timeout_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    while current < len(argv) and argv[current].startswith("-"):
        current += 1
    return current + 1 if current < len(argv) else current


def _skip_nice_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    if current < len(argv) and argv[current] == "-n":
        return current + 2
    if current < len(argv) and argv[current].startswith("-"):
        return current + 1
    return current
