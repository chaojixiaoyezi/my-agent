
# LLM: 覆盖边界（9b 2026-10-04 定）：本模块结构化拦截的是"常见写法的删除命令"——裸 rm/rmdir/unlink、
#   sh -c 一类字面 -c 程序、eval、xargs、find -delete/-exec 这些能被静态拆开的形状；命中的命令被拒并
#   引导到可恢复删除（apply_patch / task_trash）。python -c、perl -e、node -e、脚本文件等开放世界写法
#   不在覆盖范围，不做字符串扫描；真正的安全边界是执行层沙箱的写根、受保护路径和任务回收站。
from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .shell_source import shell_command_source

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
# 嵌套 Shell 程序（sh -c / eval / xargs / find -exec）递归检查的深度上限：超过按"无法解析"处理，
# fail-closed 给 finding 不放行；4 层覆盖真实常见包装，更深的写法交给沙箱边界（见文件头覆盖边界注）。
_NESTED_SHELL_DEPTH_LIMIT_COUNT = 4
# 单条嵌套程序的字符上限：解析是线性的，超长输入不再递归（fail-closed 给 finding）。32k 足够容纳真实包装文本。
_NESTED_SHELL_SOURCE_MAX_CHARS = 32_768
# 顶层命令源长度上限：64K 足够容纳真实命令行。shlex 逐字符拼 token，对单个超长参数是平方级
#   （3a 实测单个带引号长参数：64K≈35ms、128K≈0.2s、256K≈0.6s；ds5 实测 1MB≈18.4s），
#   短词很多时接近线性（64KB≈0.9ms）——按最坏形状定上限，1MB 会卡住策略线程，必须在解析前拦截。
# 3a 2026-10-05 裁定：这是解析成本的资源上界，与嵌套 Shell 源的 32K 上限同类，固定为模块常量、不做配置项。
_COMMAND_SOURCE_MAX_CHARS = 65_536


# LLM: shlex 是逐字符纯 Python 解析（1MB≈18.4s），会卡住策略线程；str 形态的顶层命令源在解析前
#   先按长度闸门拒绝，结构化报 COMMAND_SOURCE_TOO_LARGE；argv 形态不经过 shlex，不做此闸。
#   本函数是唯一长度判定入口：命令策略两条 shlex 入口与子代理受控执行网关共用它，阈值只在这里改。
# 函数用途: 检查命令源文本是否超过长度上限，超限返回一个 COMMAND_SOURCE_TOO_LARGE finding，否则返回 None。
def command_source_too_large(text: object) -> CommandPolicyFinding | None:
    if not isinstance(text, str):
        return None
    if len(text) <= _COMMAND_SOURCE_MAX_CHARS:
        return None
    return CommandPolicyFinding(
        "COMMAND_SOURCE_TOO_LARGE",
        {"limit": _COMMAND_SOURCE_MAX_CHARS, "length": len(text)},
    )

# xargs 选项表（来源：GNU findutils xargs 手册 + macOS/BSD xargs 手册，2026-10-05 shellwrap3 逐项核对）。
#   拆包的关键是"这个选项到底吃不吃下一个词"：吃错了就会把后面的命令当成选项值而跳过，真删就会放行。
#   所以只认表内完整选项名；不在表里的（含 GNU 长选项缩写）一律从严给 finding——宁可多拦，不猜缩写，
#   提示改用 `--选项=值`（明确带值、不会吃下一个词）或在命令前加 `--` 终止符。
_XARGS_LONG_VALUE_OPTIONS = frozenset(
    {"--arg-file", "--delimiter", "--max-chars", "--max-procs", "--max-args", "--process-slot-var"}
)
_XARGS_LONG_FLAG_OPTIONS = frozenset(
    {
        "--null", "--no-run-if-empty", "--verbose", "--interactive", "--open-tty", "--exit",
        "--show-limits", "--help", "--version", "--eof", "--replace", "--max-lines",
    }
)
# 短选项按字母逐个走（GNU 与 BSD 并集）：取值字母后面还有字符算连写值，没有就吃下一个词；
# 可选值字母（GNU -e/-i/-l 的 [值] 形式）后面的字符都算它的值，绝不吃下一个词；开关字母继续往后看。
_XARGS_SHORT_VALUE_LETTERS = frozenset("adEIJLnPRsS")
_XARGS_SHORT_OPTIONAL_LETTERS = frozenset("eil")
_XARGS_SHORT_FLAG_LETTERS = frozenset("0oprtx")
# find 动作：-delete 按受管删除拦；-exec/-ok 族按嵌套命令检查；写文件动作让 find 不再算只读。
_FIND_DELETE_ACTION = "-delete"
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_FIND_WRITE_ACTIONS = frozenset({"-fprint", "-fprint0", "-fprintf", "-fls"})
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
# fd 复制重定向(2>&1 / >&1 / 1>&2 / 2>&-):只改变 fd 指向,不落盘,不算副作用。
# shlex punctuation_chars=True 会把 "2>&1" 拆成 ["2", ">&", "1"],重定向目标 "1"
# 被误当成新命令段(unknown -> mutating),只读命令(ls -la 2>&1)被抬级成 mutating,
# 撞上 required action 的 read_only ceiling 后任务整体卡死(真机实证)。
_FD_COPY_REDIRECT_RE = re.compile(r"^(?:[0-9]+)?(?:>&|<&)-?[0-9]*$")


def _merge_fd_copy_redirects(argv: tuple[str, ...]) -> tuple[str, ...]:
    """把 shlex 拆散的 fd 复制重定向合并回单 token(2 >& 1 -> "2>&1")。"""
    merged: list[str] = []
    index = 0
    length = len(argv)
    while index < length:
        token = argv[index]
        if (
            token in {"0", "1", "2"}
            and index + 2 < length
            and argv[index + 1] in {">&", "<&"}
            and _FD_COPY_REDIRECT_RE.match(argv[index + 1] + argv[index + 2])
        ):
            merged.append(token + argv[index + 1] + argv[index + 2])
            index += 3
            continue
        if token in {">&", "<&"} and index + 1 < length:
            merged.append(token + argv[index + 1])
            index += 2
            continue
        merged.append(token)
        index += 1
    return tuple(merged)


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


# LLM: 在执行前以解析后的 argv 和结构化包装器规格统一拒绝删除/危险命令；新增包装器语义必须同时接入
#   顶层与嵌套 argv 检查，不能把“值看起来像参数”当作命令边界。
# 函数用途: 对一次待执行命令做静态策略判定；只返回 decision/finding，不执行命令。
def evaluate_command_policy(
    command: object,
    *,
    allow_shell_operators: bool = False,
    allowed_commands: Iterable[str] = (),
) -> CommandPolicyDecision:
    if size_finding := command_source_too_large(command):
        return CommandPolicyDecision(findings=(size_finding,))
    parsed = _parse_command_value(command)
    if parsed.findings:
        return parsed
    allowed = frozenset(allowed_commands)
    findings, covered_positions = _dangerous_pattern_findings(parsed.argv)
    if not allow_shell_operators:
        findings.extend(_shell_operator_findings(parsed.argv, _raw_command_text(command)))
    findings.extend(_dangerous_executable_findings(parsed.argv, covered_positions, allowed))
    findings.extend(_wrapper_option_findings(parsed.argv, allowed, 0))
    findings.extend(_nested_wrapper_findings(parsed.argv, allowed, 0))
    return CommandPolicyDecision(parsed.argv, _unique_findings(findings))


# LLM: 只读分析入口（不执行命令）：先过长度闸门，再依次做解析、策略判定与段结构分类；解析或策略
#   有 finding 时归为 unknown/dangerous，调用方不得把 unknown 当只读放行。
# 函数用途: 解析命令的记号、操作符与重定向，返回命令分类与理由码。
def analyze_command(command: object) -> CommandAnalysis:
    """Parse commands, operators and redirections; unknowns never become read-only."""

    if size_finding := command_source_too_large(command):
        return CommandAnalysis("unknown", reason_codes=(size_finding.code,))
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
        if executable == "git":
            sub = _git_subcommand(args)
            if sub in {"branch", "diff", "log", "rev-parse", "show", "status"}:
                return "read_only"
            if sub in {"clone", "fetch", "pull"}:
                # 网络接收 + 沙箱内本地写，不是外发：与 mkdir/cp 同级 mutating。
                # 归 dangerous 会让下载源码在 approval 无消费端的通道里卡死
                # （真机铁证 2026-08-08：celery 复刻 git clone → APPROVAL_REQUIRED）。
                return "mutating"
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
    if executable == "find" and _find_has_write_action(args):
        # find 带删除/执行/写文件动作时不再算只读；-delete 的删除 finding 在策略层单独拦。
        # 嵌套分类只按"更严"方向影响外层：外层包装（sh/bash/xargs）的分类不低于 unknown，
        # 嵌套只读不会把外层降级，嵌套危险由 findings 路径覆盖为 dangerous。
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
        argv = _merge_fd_copy_redirects(tuple(lexer))
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
    merged = _merge_fd_copy_redirects(tuple(argv))
    return CommandPolicyDecision(merged, () if merged else (CommandPolicyFinding("COMMAND_EMPTY"),))


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


# LLM: 包一层 shell（sh -c / eval / xargs / find -exec）会把删除命令藏进参数文本；这里用与顶层相同的
#   解析器和危险模式/受管删除检查递归拆包，超深或超长按"无法解析"fail-closed。只做结构化提取，
#   不扫原始字符串；python -c / perl -e / 脚本文件是开放世界，不在覆盖范围（安全边界是 OS 沙箱）。
# 函数用途: 找出 argv 里的嵌套程序（sh -c、eval、xargs、find 动作）并递归检查，返回带 nested_depth 的 finding。
def _nested_wrapper_findings(
    argv: tuple[str, ...],
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    findings: list[CommandPolicyFinding] = []
    for position in effective_command_positions(argv):
        findings.extend(_wrapper_position_findings(argv, position, allowed_commands, depth))
    return findings


# 函数用途: 检查一个命令位是不是嵌套包装（sh -c / eval / xargs / find），是则交给对应拆包检查。
def _wrapper_position_findings(
    argv: tuple[str, ...],
    position: int,
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    executable = command_name(argv[position])
    args = _command_args(argv, position)
    if executable in _SHELL_EXECUTABLES:
        source = shell_command_source(tuple(args))
        return _nested_program_findings(source, allowed_commands, depth + 1) if source is not None else []
    if executable == "eval":
        source = " ".join(args).strip()
        return _nested_program_findings(source, allowed_commands, depth + 1) if source else []
    if executable == "xargs":
        return _xargs_findings(args, allowed_commands, depth + 1)
    if executable == "find":
        # find 的动作扫描用从命令位到 argv 末尾的完整序列（不按 shell operator 截断）：
        # `;` 在 find 语法里是 -exec 段终止符，转义 `\;` 解析后与 shell 分隔符同形，
        # 按截断处理会丢掉第二个及以后的 -exec 段（shellwrapr 初审漏 2）；宁严跨段继续扫。
        return _find_action_findings(list(argv[position + 1:]), allowed_commands, depth + 1)
    return []


# 函数用途: 解析一条嵌套 Shell 程序文本并检查危险模式、受管删除和更深嵌套；超深/超长/解析失败 fail-closed。
def _nested_program_findings(
    source: str,
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    if depth > _NESTED_SHELL_DEPTH_LIMIT_COUNT:
        return [CommandPolicyFinding("COMMAND_NESTED_DEPTH_EXCEEDED", {"nested_depth": depth})]
    if len(source) > _NESTED_SHELL_SOURCE_MAX_CHARS:
        return [CommandPolicyFinding("COMMAND_NESTED_SOURCE_TOO_LARGE", {"nested_depth": depth})]
    parsed = _parse_command_value(source)
    if parsed.findings:
        return _tag_nested_depth(list(parsed.findings), depth)
    return _nested_argv_findings(parsed.argv, allowed_commands, depth)


# LLM: 嵌套 argv 是 sh/eval/xargs/find 或命令字符串选项复用的统一检查入口；unknown wrapper option 必须同样
#   fail-closed，新增解析路径须保留 depth 计数和同一 allowlist。
# 函数用途: 对嵌套 argv 检查危险模式、受管删除、包装器选项和更深嵌套。
def _nested_argv_findings(
    argv: tuple[str, ...],
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    if depth > _NESTED_SHELL_DEPTH_LIMIT_COUNT:
        return [CommandPolicyFinding("COMMAND_NESTED_DEPTH_EXCEEDED", {"nested_depth": depth})]
    findings, covered_positions = _dangerous_pattern_findings(argv)
    findings.extend(_dangerous_executable_findings(argv, covered_positions, allowed_commands))
    findings.extend(_wrapper_option_findings(argv, allowed_commands, depth))
    findings.extend(_nested_wrapper_findings(argv, allowed_commands, depth))
    return _tag_nested_depth(findings, depth)


# 函数用途: 拆开 xargs 的选项，把其后的命令按嵌套 argv 检查；未知选项从严给 finding 不放行。
def _xargs_findings(
    args: list[str],
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    index, option_findings = _xargs_scan_options(args)
    if index >= len(args):
        return option_findings
    nested = _nested_argv_findings(tuple(args[index:]), allowed_commands, depth)
    return [*option_findings, *nested]


# LLM: 选项解析必须是线性一遍扫描：每读一个选项就决定它是否吃掉下一个词，不做"多种解释"的分叉
#   （嵌套 xargs 时分叉组合会指数爆炸）。长短选项分开处理，表外的选项一律从严（见选项表注释）。
# 函数用途: 扫描 xargs 的选项，返回命令起始下标与需要拦截的未知选项 finding。
def _xargs_scan_options(args: list[str]) -> tuple[int, list[CommandPolicyFinding]]:
    findings: list[CommandPolicyFinding] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return index + 1, findings
        if not token.startswith("-") or token == "-":
            return index, findings
        if token.startswith("--"):
            index = _xargs_after_long_option(args, index, findings)
        else:
            index = _xargs_after_short_options(args, index, findings)
    return index, findings


# LLM: 长选项只认表内完整名：`--opt=value` 无歧义（不吃下一个词）整体跳过；必须带值的吃下一个词；
#   开关/可选值不吃；表外的（含 GNU 缩写）给 finding——缩写可能属于带值选项，猜错就会放走命令。
# 函数用途: 按已知长选项表决定跳过几个 token；未知选项追加一条拦截 finding。
def _xargs_after_long_option(
    args: list[str], index: int, findings: list[CommandPolicyFinding]
) -> int:
    token = args[index]
    if "=" in token:
        return index + 1
    if token in _XARGS_LONG_VALUE_OPTIONS:
        return index + 2 if index + 1 < len(args) else index + 1
    if token in _XARGS_LONG_FLAG_OPTIONS:
        return index + 1
    findings.append(_xargs_unknown_option_finding(token))
    return index + 1


# LLM: 短选项串按字母逐个走：取值字母后面还有字符就是连写值（同一 token），没有就吃下一个词；
#   可选值字母（-e/-i/-l）后面的字符都算它的值、绝不吃下一个词；开关字母继续；未知字母给 finding。
# 函数用途: 解析一个短选项串，返回下一个待读下标；未知字母追加一条拦截 finding。
def _xargs_after_short_options(
    args: list[str], index: int, findings: list[CommandPolicyFinding]
) -> int:
    letters = args[index][1:]
    for position, letter in enumerate(letters):
        if letter in _XARGS_SHORT_VALUE_LETTERS:
            # 后面还有字符就是连写值（同一 token）；没有就吃下一个词（越界则到尾部为止）。
            consumed = index + 1 if letters[position + 1 :] else index + 2
            return min(consumed, len(args))
        if letter in _XARGS_SHORT_OPTIONAL_LETTERS:
            return index + 1
        if letter not in _XARGS_SHORT_FLAG_LETTERS:
            findings.append(_xargs_unknown_option_finding(args[index]))
            return index + 1
    return index + 1


# 函数用途: 生成 xargs 未知选项的拦截 finding，附可恢复的两种明确写法建议。
def _xargs_unknown_option_finding(token: str) -> CommandPolicyFinding:
    return CommandPolicyFinding(
        "COMMAND_XARGS_UNKNOWN_OPTION",
        {
            "option": str(token)[:40],
            "recovery": "use --option=value or -- before the command",
        },
    )


# 函数用途: 检查 find 的 -delete（按受管删除）与 -exec/-execdir/-ok/-okdir（按嵌套命令）；返回 finding。
def _find_action_findings(
    args: list[str],
    allowed_commands: frozenset[str],
    depth: int,
) -> list[CommandPolicyFinding]:
    findings: list[CommandPolicyFinding] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == _FIND_DELETE_ACTION:
            findings.append(_find_delete_finding(depth))
            index += 1
            continue
        if token in _FIND_EXEC_ACTIONS:
            segment_findings, index = _find_exec_findings(args, index + 1, allowed_commands, depth)
            findings.extend(segment_findings)
            continue
        index += 1
    return findings


# 函数用途: 生成 find -delete 的受管删除 finding（与嵌套 rm 同一 code 与替换建议）。
def _find_delete_finding(depth: int) -> CommandPolicyFinding:
    return CommandPolicyFinding(
        "COMMAND_DESTRUCTIVE_DELETE_BLOCKED",
        {
            "executable": "find",
            "action": _FIND_DELETE_ACTION,
            "replacement": "apply_patch_or_task_trash",
            "nested_depth": depth,
        },
    )


# 函数用途: 检查一段 find -exec 嵌套命令；返回 finding 与下一个待扫描位置。
def _find_exec_findings(
    args: list[str],
    start: int,
    allowed_commands: frozenset[str],
    depth: int,
) -> tuple[list[CommandPolicyFinding], int]:
    segment, next_index = _find_exec_segment(args, start)
    if not segment:
        return [], next_index
    return _nested_argv_findings(segment, allowed_commands, depth), next_index


# 函数用途: 取 find -exec 动作后的嵌套命令（到 + 或段尾）；返回命令段与下一个待扫描位置。
def _find_exec_segment(args: list[str], start: int) -> tuple[tuple[str, ...], int]:
    segment: list[str] = []
    index = start
    while index < len(args):
        token = args[index]
        if token in {";", "+"}:
            return tuple(segment), index + 1
        segment.append(token)
        index += 1
    return tuple(segment), index


# 函数用途: 给嵌套层产生的 finding 补 nested_depth；更深处已带的 finding 不被覆盖。
def _tag_nested_depth(
    findings: list[CommandPolicyFinding],
    depth: int,
) -> list[CommandPolicyFinding]:
    return [
        finding
        if "nested_depth" in finding.evidence
        else replace(finding, evidence={**finding.evidence, "nested_depth": depth})
        for finding in findings
    ]


# 函数用途: 判断 find 是否带删除/执行/写文件动作；带则不再算只读。
def _find_has_write_action(args: list[str]) -> bool:
    return any(
        token == _FIND_DELETE_ACTION or token in _FIND_EXEC_ACTIONS or token in _FIND_WRITE_ACTIONS
        for token in args
    )


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


# LLM: 前缀运行器规格只描述选项、位置参数及可嵌套的命令字符串值；它不执行命令，策略入口负责将命令值
#   送进统一嵌套检查，避免把运行器的 shell 程序误当普通数据。
# 类用途: 登记一个前缀运行器的选项形态：取值选项（吃一个词）、开关选项、固定位置参数个数、
#   是否跳过 NAME=VALUE 与 "-"（env/sudo 支持），以及值本身是一条命令的选项。
@dataclass(frozen=True)
class _CommandWrapperSpec:
    value_options: frozenset[str] = frozenset()
    flag_options: frozenset[str] = frozenset()
    positional_count: int = 0
    assignments: bool = False
    command_options: frozenset[str] = frozenset()


# LLM: 前缀运行器表（来源：各命令手册，2026-10-05 shellwrap4 逐项核对——sudo/env/nice/timeout/nohup/
#   time/command/exec/builtin 为 POSIX/coreutils 与 sudo 手册，stdbuf 为 coreutils，ionice/chrt/
#   taskset/setsid/flock 为 util-linux，caffeinate 为 macOS，unbuffer 为 expect）。拆包的关键是
#   "这个运行器吃掉几个词才能看到真正的命令"：吃错了内层 rm 就会逃过检查。命令字符串选项单独标记，
#   其值会进入同一嵌套解析；位置参数按手册登记（timeout 时长、flock 锁文件等）。表外选项从严，不猜是否吃值。
_COMMAND_WRAPPER_SPECS: dict[str, _CommandWrapperSpec] = {
    "sudo": _CommandWrapperSpec(
        frozenset(
            {"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-U", "-T", "--user", "--group",
             "--prompt", "--chdir", "--chroot", "--role", "--type", "--other-user",
             "--command-timeout", "--close-from"}
        ),
        frozenset(
            {"-i", "-s", "-n", "-k", "-A", "-E", "-b", "-H", "-P", "-S", "-e", "-v", "-l", "-V",
             "--login", "--shell", "--non-interactive", "--reset-timestamp", "--remove-timestamp",
             "--askpass", "--preserve-env", "--set-home", "--stdin", "--edit", "--validate",
             "--list", "--version", "--help", "--background", "--bell"}
        ),
        assignments=True,
    ),
    "env": _CommandWrapperSpec(
        frozenset({"-u", "-C", "--unset", "--chdir"}),
        frozenset({"-i", "-0", "-v", "--ignore-environment", "--null", "--debug"}),
        assignments=True,
        command_options=frozenset({"-S", "--split-string"}),
    ),
    "nice": _CommandWrapperSpec(frozenset({"-n", "--adjustment"})),
    "timeout": _CommandWrapperSpec(
        frozenset({"-k", "-s", "--kill-after", "--signal"}),
        frozenset({"--preserve-status", "--foreground", "-v", "--verbose"}),
        positional_count=1,
    ),
    "nohup": _CommandWrapperSpec(),
    "time": _CommandWrapperSpec(flag_options=frozenset({"-p", "--portability"})),
    "command": _CommandWrapperSpec(flag_options=frozenset({"-p", "-v", "-V"})),
    "exec": _CommandWrapperSpec(frozenset({"-a"}), frozenset({"-c", "-l"})),
    "builtin": _CommandWrapperSpec(),
    "stdbuf": _CommandWrapperSpec(
        frozenset({"-i", "-o", "-e", "--input", "--output", "--error"})
    ),
    "ionice": _CommandWrapperSpec(
        frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
        frozenset({"-t", "--ignore"}),
    ),
    "chrt": _CommandWrapperSpec(
        frozenset(
            {"-p", "-T", "-P", "-D", "--pid", "--sched-runtime", "--sched-period", "--sched-deadline"}
        ),
        frozenset(
            {"-b", "-d", "-f", "-i", "-o", "-r", "-a", "-m", "-R", "-v", "-V", "--batch",
             "--deadline", "--fifo", "--idle", "--other", "--rr", "--all-tasks", "--max",
             "--reset-on-fork", "--verbose", "--version"}
        ),
        positional_count=1,
    ),
    "taskset": _CommandWrapperSpec(
        frozenset(),
        frozenset({"-a", "-c", "-p", "-h", "-V", "--all-tasks", "--cpu-list", "--pid",
                   "--help", "--version"}),
        positional_count=1,
    ),
    "setsid": _CommandWrapperSpec(
        flag_options=frozenset({"-c", "-f", "-w", "--ctty", "--fork", "--wait"})
    ),
    "flock": _CommandWrapperSpec(
        frozenset({"-w", "-E", "--wait", "--timeout", "--conflict-exit-code"}),
        frozenset({"-s", "-x", "-n", "-o", "-u", "-F", "--shared", "--exclusive", "--nonblock",
                   "--close", "--unlock", "--no-fork"}),
        positional_count=1,
        command_options=frozenset({"-c", "--command"}),
    ),
    "caffeinate": _CommandWrapperSpec(
        frozenset({"-t", "-w"}),
        frozenset({"-d", "-i", "-m", "-s", "-u"}),
    ),
    "unbuffer": _CommandWrapperSpec(flag_options=frozenset({"-p"})),
}


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
        if _FD_COPY_REDIRECT_RE.match(token):
            # fd 复制重定向(2>&1 等)不是命令段,也不打断命令序列。
            continue
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


# LLM: 表驱动的前缀解包沿运行器链前进，收集未知选项和标记为命令的值；未知选项不吞下一个词，
#   命令值由调用方按统一深度限制递归检查。解析只向前扫描。
# 函数用途: 返回内层命令位置、未知选项和待嵌套解析的命令字符串。
def _unwrap_wrapper_chain(
    argv: tuple[str, ...], position: int
) -> tuple[int, list[str], list[str]]:
    current = position
    unknown: list[str] = []
    command_sources: list[str] = []
    while current < len(argv):
        spec = _COMMAND_WRAPPER_SPECS.get(command_name(argv[current]))
        if spec is None:
            return current, unknown, command_sources
        next_position, chain_unknown, chain_sources = _skip_wrapper_options(argv, current, spec)
        unknown.extend(chain_unknown)
        command_sources.extend(chain_sources)
        if next_position <= current:
            return current, unknown, command_sources
        current = next_position
    return position, unknown, command_sources


def _unwrap_command_position(argv: tuple[str, ...], position: int) -> int:
    return _unwrap_wrapper_chain(argv, position)[0]


# LLM: `--` 只终止选项解析，不终止 env/sudo 规格声明的 NAME=VALUE 扫描；命令字符串选项捕获原始参数值，
#   不在此处执行或按字符串关键字分类。
# 函数用途: 按 spec 跳过选项、赋值与固定位置参数，返回内层命令位置及待检查信息。
def _skip_wrapper_options(
    argv: tuple[str, ...], position: int, spec: _CommandWrapperSpec
) -> tuple[int, list[str], list[str]]:
    index = position + 1
    remaining = spec.positional_count
    unknown: list[str] = []
    command_sources: list[str] = []
    options_ended = False
    while index < len(argv):
        token = argv[index]
        if not options_ended and token == "--":
            options_ended = True
            index += 1
            continue
        if not options_ended and token.startswith("-") and token != "-":
            index, option_unknown, command_source = _skip_wrapper_option_token(argv, index, spec)
            unknown.extend((option_unknown,) if option_unknown is not None else ())
            command_sources.extend((command_source,) if command_source is not None else ())
            continue
        if spec.assignments and (token == "-" or looks_like_assignment(token)):
            index += 1
            continue
        if remaining <= 0:
            return index, unknown, command_sources
        remaining -= 1
        index += 1
    return index, unknown, command_sources


# LLM: 只按规格表的完整选项名识别选项；`--name=value` 仅当 name 确实登记时才消费，
#   命令字符串值单独返回供嵌套解析，未知项保留原 token 并 fail-closed。
# 函数用途: 解析一个运行器选项，返回下标、未知选项及可选命令字符串值。
def _skip_wrapper_option_token(
    argv: tuple[str, ...], index: int, spec: _CommandWrapperSpec
) -> tuple[int, str | None, str | None]:
    token = argv[index]
    if token in spec.command_options:
        if index + 1 < len(argv):
            return index + 2, None, argv[index + 1]
        return index + 1, token, None
    if token in spec.value_options:
        return (index + 2 if index + 1 < len(argv) else index + 1), None, None
    if token in spec.flag_options:
        return index + 1, None, None
    if "=" in token:
        option, _separator, value = token.partition("=")
        if option in spec.command_options:
            return index + 1, None, value
        if option in spec.value_options:
            return index + 1, None, None
        return index + 1, token, None
    command_value = _wrapper_option_attached_value(token, spec.command_options)
    if command_value is not None:
        return index + 1, None, command_value
    if _wrapper_option_attached_value(token, spec.value_options) is not None:
        # 取值选项粘连值（-n10、-oL）：值在同一 token 里，不额外吃词。
        return index + 1, None, None
    return index + 1, token, None


# LLM: 仅对登记为取值选项的短选项识别粘连值，长选项必须精确匹配；未知 token 由调用方 fail-closed。
# 函数用途: 提取短选项 token 中已登记选项后面的粘连值。
def _wrapper_option_attached_value(token: str, options: frozenset[str]) -> str | None:
    if token.startswith("--"):
        return None
    for option in options:
        if not option.startswith("--") and len(token) > len(option) and token.startswith(option):
            return token[len(option) :]
    return None


# LLM: 包装器的未知选项与命令字符串必须在顶层和嵌套 argv 共用此入口；字符串只送入现有嵌套解析器，
#   深度从当前层加一并沿原上限 fail-closed。
# 函数用途: 收集未知选项 finding，并递归验证规格标记为命令的选项值。
def _wrapper_option_findings(
    argv: tuple[str, ...], allowed_commands: frozenset[str], depth: int
) -> list[CommandPolicyFinding]:
    findings: list[CommandPolicyFinding] = []
    for position in command_positions(argv):
        _inner, unknown, command_sources = _unwrap_wrapper_chain(argv, position)
        findings.extend(_wrapper_unknown_option_finding(token) for token in unknown)
        for source in command_sources:
            findings.extend(_nested_program_findings(source, allowed_commands, depth + 1))
    return findings


# 函数用途: 生成前缀运行器表外选项的拦截 finding，附可恢复的写法建议。
def _wrapper_unknown_option_finding(token: str) -> CommandPolicyFinding:
    return CommandPolicyFinding(
        "COMMAND_WRAPPER_UNKNOWN_OPTION",
        {
            "option": str(token)[:40],
            "recovery": "remove the option or put -- before the command",
        },
    )
