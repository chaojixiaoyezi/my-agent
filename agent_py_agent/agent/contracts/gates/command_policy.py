
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_CATASTROPHIC_EXECUTABLES = frozenset({"shutdown", "reboot", "halt", "poweroff", "telinit"})
_MANAGED_DELETE_EXECUTABLES = frozenset({"rm", "rmdir", "unlink"})
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
_READ_ONLY_EXECUTABLES = frozenset(
    {
        "basename",
        "cat",
        "cmp",
        "cut",
        "diff",
        "dirname",
        "du",
        "env",
        "file",
        "find",
        "git",
        "grep",
        "head",
        "id",
        "jq",
        "ls",
        "md5",
        "md5sum",
        "pwd",
        "pytest",
        "rg",
        "sed",
        "sha1sum",
        "sha256sum",
        "sort",
        "stat",
        "tail",
        "test",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)
_KNOWN_MUTATING_EXECUTABLES = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "install",
        "ln",
        "mkdir",
        "mv",
        "patch",
        "python",
        "python3",
        "ruby",
        "tee",
        "touch",
    }
)
_EXTERNAL_SEND_EXECUTABLES = frozenset(
    {"curl", "ftp", "git", "nc", "netcat", "rsync", "scp", "sftp", "ssh", "wget"}
)
_REDIRECTION_OPERATORS = frozenset({">", ">>", "<>", "2>", "2>>", "&>", "&>>"})
_SEQUENCE_OPERATORS = frozenset({";", "&&", "||", "|", "&"})


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


@dataclass(frozen=True)
class CommandSegment:
    executable: str
    argv: tuple[str, ...]
    operator_before: str = ""


@dataclass(frozen=True)
class CommandAnalysis:
    """Deterministic command classification used by ActionPolicy."""

    classification: str
    argv: tuple[str, ...] = ()
    segments: tuple[CommandSegment, ...] = ()
    reason_codes: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    @property
    def resolved_effect(self) -> str:
        if self.classification == "read_only":
            return "read_only"
        if self.classification == "dangerous":
            return "dangerous"
        return "mutating"


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


def analyze_command(command: object) -> CommandAnalysis:
    """Parse commands, operators and redirections; unknowns never become read-only."""

    parsed = _parse_command_value(command)
    if parsed.findings:
        return CommandAnalysis(
            "unknown",
            reason_codes=tuple(item.code for item in parsed.findings),
        )
    policy = evaluate_command_policy(command, allow_shell_operators=True)
    if policy.findings:
        return CommandAnalysis(
            "dangerous",
            argv=parsed.argv,
            segments=_command_segments(parsed.argv),
            reason_codes=policy.finding_codes,
            paths=_command_path_candidates(parsed.argv),
        )
    segments = _command_segments(parsed.argv)
    if not segments:
        return CommandAnalysis("unknown", argv=parsed.argv, reason_codes=("COMMAND_EMPTY",))
    redirections = tuple(token for token in parsed.argv if token in _REDIRECTION_OPERATORS)
    if redirections:
        return CommandAnalysis(
            "mutating",
            argv=parsed.argv,
            segments=segments,
            reason_codes=("COMMAND_REDIRECTION_MUTATING",),
            paths=_command_path_candidates(parsed.argv),
        )
    classifications = tuple(_segment_classification(segment) for segment in segments)
    if "dangerous" in classifications:
        classification = "dangerous"
    elif "unknown" in classifications:
        classification = "unknown"
    elif "mutating" in classifications:
        classification = "mutating"
    else:
        classification = "read_only"
    reasons = tuple(
        f"COMMAND_SEGMENT_{item.upper()}" for item in dict.fromkeys(classifications)
    )
    return CommandAnalysis(
        classification,
        argv=parsed.argv,
        segments=segments,
        reason_codes=reasons,
        paths=_command_path_candidates(parsed.argv),
    )


def _command_segments(argv: tuple[str, ...]) -> tuple[CommandSegment, ...]:
    positions = command_positions(argv)
    segments: list[CommandSegment] = []
    for index, position in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else len(argv)
        start = _unwrap_command_position(argv, position)
        operator = argv[position - 1] if position > 0 and is_shell_operator_token(argv[position - 1]) else ""
        segment_argv = tuple(
            token for token in argv[start:end] if token not in _SEQUENCE_OPERATORS
        )
        if not segment_argv:
            continue
        segments.append(
            CommandSegment(command_name(segment_argv[0]), segment_argv, operator)
        )
    return tuple(segments)


def _segment_classification(segment: CommandSegment) -> str:
    executable = segment.executable
    args = segment.argv[1:]
    if executable in _CATASTROPHIC_EXECUTABLES or _is_dangerous_executable(executable):
        return "dangerous"
    if executable in _MANAGED_DELETE_EXECUTABLES:
        return "dangerous"
    if executable in _EXTERNAL_SEND_EXECUTABLES:
        if executable == "git" and _git_subcommand(args) in {
            "branch",
            "diff",
            "log",
            "rev-parse",
            "show",
            "status",
        }:
            return "read_only"
        return "dangerous"
    if executable == "git":
        return (
            "read_only"
            if _git_subcommand(args)
            in {"branch", "diff", "log", "rev-parse", "show", "status"}
            else "mutating"
        )
    if executable == "sed" and any(arg in {"-i", "--in-place"} or arg.startswith("-i") for arg in args):
        return "mutating"
    if executable in {"python", "python3"} and _python_read_only_module(args):
        return "read_only"
    if executable in _READ_ONLY_EXECUTABLES:
        return "read_only"
    if executable in _KNOWN_MUTATING_EXECUTABLES:
        return "mutating"
    return "unknown"


def _python_read_only_module(args: tuple[str, ...]) -> bool:
    """Recognize only explicitly enumerated inspection modules.

    Arbitrary Python remains mutating/unknown because source text and scripts
    can perform any effect.  ``python -m pytest`` is a bounded exception used
    for test execution; OS sandboxing still controls its filesystem/network.
    """

    try:
        marker = args.index("-m")
    except ValueError:
        return False
    return marker + 1 < len(args) and args[marker + 1].lower() == "pytest"


def _git_subcommand(args: tuple[str, ...]) -> str:
    for item in args:
        if not item.startswith("-"):
            return item.lower()
    return ""


def _command_path_candidates(argv: tuple[str, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    for token in argv:
        if token in _SEQUENCE_OPERATORS or token in _REDIRECTION_OPERATORS:
            continue
        if token.startswith(("/", "./", "../", "~/")) and token not in paths:
            paths.append(token)
    return tuple(paths)


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
    allowed_executables = frozenset(command_name(item) for item in allowed_commands)
    for position in effective_command_positions(argv):
        if position in covered_positions:
            continue
        executable = command_name(argv[position])
        if executable in _MANAGED_DELETE_EXECUTABLES and executable not in allowed_executables:
            findings.append(
                CommandPolicyFinding(
                    "COMMAND_DESTRUCTIVE_DELETE_BLOCKED",
                    {
                        "executable": executable,
                        "replacement": "apply_patch_or_task_trash",
                    },
                )
            )
            continue
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
    "CommandAnalysis",
    "CommandPolicyDecision",
    "CommandPolicyFinding",
    "CommandSegment",
    "analyze_command",
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
