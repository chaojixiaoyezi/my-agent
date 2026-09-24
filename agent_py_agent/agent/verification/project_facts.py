from __future__ import annotations

"""Detect canonical project checks and classify commands without guessing intent.

LLM: this is the single verifier-command detector.  It reads manifest facts and
exact shell tokens; it never treats user prose or a command's output as authority.
模块用途: 找到代码项目根目录和项目已经声明的测试命令，用于区分局部与全量验证。
"""

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "pytest.ini",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)
_JS_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"),
    ("bun.lock", "bun"),
    ("bun.lockb", "bun"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
)
_VERIFY_TARGETS = ("test", "tests", "lint", "typecheck", "check", "build", "fmt", "format")
# 捕获分隔符本身，才能区分 && 与 ;、||：后两者会让前一段的失败被掩盖。
_CHAIN_SPLIT_RE = re.compile(r"\s*(&&|\|\||;)\s*")
# 铁律：一次返回码只能证明一条命令。管道取末段返回码、后台立即返回 0，返回码都不再属于测试命令本身，一律不算证据。
_EXIT_CODE_HIDING_TOKENS = frozenset({"|", "|&", "&"})
# 同一铁律决定放行边界：只放行开头一个 `cd <目录> &&`。&& 在 cd 失败时短路，而目录存在且可进入由文件系统事实判定
# （不解析输出），这时返回码只可能来自后一条命令；其余链式写法仍拒绝。
_CD_PREFIX_RE = re.compile(r"^\s*cd\s+(?P<target>\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s*&&\s*(?P<rest>.+)$", re.S)
# 返回码 126/127 是 shell 约定的"不可执行/找不到命令"：验证命令根本没有运行，只按返回码判定，不解析输出。
_NOT_RUN_EXIT_CODES = frozenset({126, 127})
ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
# pytest 按参数形状判范围：文件、::node 或这些筛选开关只跑部分用例（targeted）；目录或不带路径按 full。
_PYTEST_SELECTION_FLAGS = frozenset({"-k", "-m", "--lf", "--last-failed", "--deselect"})
_MAX_FACT_FILE_BYTES = 256 * 1024
_MAX_VERIFY_COMMANDS = 8
_MAX_OUTPUT_SUMMARY_CHARS = 2000


@dataclass(frozen=True)
class ProjectFacts:
    """Structured project root and its declared verification commands."""

    root: Path
    verify_commands: tuple[str, ...]


@dataclass(frozen=True)
class ClassifiedVerification:
    """A real command result classified against project facts."""

    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    root: str
    output_summary: str


# LLM: root discovery is bounded to ancestors and exact marker names; do not add
# source-text keyword scans or infer a project from the user's request.
# 函数用途: 从工作目录向上找到最近的代码项目根目录。
def project_facts_for(cwd: str | Path | None) -> ProjectFacts | None:
    try:
        start = Path(cwd or ".").expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if start.is_file():
        start = start.parent
    candidates = (start, *start.parents)
    root = next((path for path in candidates if (path / ".git").exists()), None)
    if root is None:
        root = next(
            (path for path in candidates if any((path / marker).is_file() for marker in _PROJECT_MARKERS[1:])),
            None,
        )
    if root is None:
        return None
    return ProjectFacts(root=root, verify_commands=tuple(_verify_commands(root)))


# LLM: only exact commands discovered from the current project can become evidence; a successful
# arbitrary shell command is not verification.  One shell exit code proves one command, so a single
# command is classified by its code, while an `&&` chain proves every segment only when the whole
# chain returns 0 (a non-zero code cannot be attributed to one segment and yields nothing).  A single
# leading `cd <existing dir> &&` moves the project lookup and recorded cwd to that directory.
# 函数用途: 把一次真实命令结果归类成验证证据列表；单条命令按返回码，&& 串联整体为 0 时每段记通过，其余情况为空。
def classify_verification_commands(
    command: str,
    *,
    cwd: str | Path | None,
    exit_code: int,
    output: str,
) -> list[ClassifiedVerification]:
    if not isinstance(command, str) or not command.strip():
        return []
    effective_cwd, body = _cd_prefix(command, cwd)
    facts = project_facts_for(effective_cwd)
    if facts is None or not facts.verify_commands or body is None:
        return []
    segments = _command_segments(body)
    if len(segments) > 1 and int(exit_code) != 0:
        return []
    matches = [match for match in (_canonical_match(tokens, facts.verify_commands) for tokens in segments) if match]
    return [
        ClassifiedVerification(
            command=command,
            canonical_command=canonical,
            kind=_kind_for_command(canonical),
            scope=_scope_for(canonical, trailing_args),
            status=_status_for(int(exit_code)),
            exit_code=int(exit_code),
            cwd=str(Path(effective_cwd or ".").expanduser().resolve(strict=False)),
            root=str(facts.root),
            output_summary=_summarize_output(output),
        )
        for canonical, trailing_args in matches
    ]


# LLM: 只比对一段已拆好的记号；返回首个匹配的规范命令及其后的参数，供范围判断。不看输出、不猜用户意图。
# 函数用途: 判断一段命令是否是项目声明的某条规范验证命令。
def _canonical_match(
    tokens: list[str],
    canonical_commands: tuple[str, ...],
) -> tuple[str, list[str]] | None:
    candidate = _strip_prefixes(tokens)
    spellings = ((canonical, spelling) for canonical in canonical_commands
                 for spelling in _equivalent_spellings(_tokens(canonical)))
    return next(((canonical, candidate[len(spelling) :]) for canonical, spelling in spellings
                 if candidate[: len(spelling)] == spelling), None)


# LLM: 状态只由返回码决定：0 通过，126/127 表示命令没运行（环境不可用，不算测试失败），其余非零为失败。
# 函数用途: 把返回码映射为验证状态。
def _status_for(exit_code: int) -> str:
    if exit_code == 0:
        return "passed"
    return ENVIRONMENT_UNAVAILABLE if exit_code in _NOT_RUN_EXIT_CODES else "failed"


# LLM: pytest 按参数形状判定：含 ::、以 .py 结尾，或带 -k/-m/--lf/--deselect 等筛选开关为 targeted；只给目录
#   或不给路径为 full（目录参数按约定算 full，不再逐一核对它是否覆盖全部测试）。其它生态沿用原 _looks_like_target 规则。
# 函数用途: 根据规范命令和其后的参数判断这次验证是局部还是全量。
def _scope_for(canonical: str, trailing_args: list[str]) -> str:
    if canonical == "pytest":
        return "targeted" if any(_pytest_selects_subset(arg) for arg in trailing_args) else "full"
    return "targeted" if any(_looks_like_target(arg) for arg in trailing_args) else "full"


# LLM: 只看参数本身的形状，不读取文件系统或配置；-kEXPR/-mEXPR 这类连写与 --deselect=… 一并识别。
# 函数用途: 判断一个 pytest 参数是否只选中部分用例。
def _pytest_selects_subset(arg: str) -> bool:
    if "::" in arg or arg.endswith(".py") or arg in _PYTEST_SELECTION_FLAGS:
        return True
    return arg.startswith("--deselect=") or (arg[:2] in {"-k", "-m"} and not arg.startswith("--"))


# LLM: known manifest defaults are structured fast paths, while package/Makefile declarations remain open extension points;
# add ecosystems here only when their canonical verifier is stable and independently classifiable from exact command tokens.
# 函数用途: 根据项目清单列出可作为真实验证证据的标准命令，不读取用户提示或模型总结。
def _verify_commands(root: Path) -> list[str]:
    verify: list[str] = []
    if (root / "scripts" / "run_tests.sh").is_file():
        verify.append("scripts/run_tests.sh")
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(_read_small(package_json) or "{}").get("scripts") or {}
        except (AttributeError, json.JSONDecodeError):
            scripts = {}
        package_manager = next((name for lock, name in _JS_LOCKFILES if (root / lock).is_file()), "npm")
        verify.extend(
            f"{package_manager} run {name}"
            for name in _VERIFY_TARGETS
            if isinstance(scripts, dict) and name in scripts
        )
    if (root / "pytest.ini").is_file() or "[tool.pytest" in _read_small(root / "pyproject.toml"):
        verify.append("pytest")
    if (root / "go.mod").is_file():
        verify.extend(("go test", "go build"))
    if (root / "Cargo.toml").is_file():
        verify.extend(("cargo test", "cargo check", "cargo build"))
    makefile = _read_small(root / "Makefile")
    if makefile:
        verify.extend(
            f"make {name}"
            for name in _VERIFY_TARGETS
            if re.search(rf"^{re.escape(name)}\s*:", makefile, re.MULTILINE)
        )
    return list(dict.fromkeys(verify))[:_MAX_VERIFY_COMMANDS]


def _read_small(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FACT_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# LLM: 依"一次返回码只能证明一条命令"，只接受开头一个 `cd <目标> && <其余>`；目标按原 cwd 解析，是否为可进入的
#   现有目录只看文件系统事实，不解析命令输出；否则返回 (cwd, None) 表示不可作为证据。没有 cd 前缀时原样返回，
#   其余段由 _command_segments 按 && 规则拆分。
# 函数用途: 拆出命令开头的 cd 前缀，返回实际工作目录和剩余命令文本。
def _cd_prefix(command: str, cwd: str | Path | None) -> tuple[str | Path | None, str | None]:
    match = _CD_PREFIX_RE.match(command)
    if match is None:
        return cwd, command
    try:
        (target,) = shlex.split(match.group("target"))
        directory = (Path(cwd or ".").expanduser() / Path(target).expanduser()).resolve(strict=False)
    except (ValueError, OSError, RuntimeError):
        return cwd, None
    if not directory.is_dir() or not os.access(directory, os.X_OK):
        return cwd, None
    return directory, match.group("rest")


# LLM: 按带引号语义的 shell 记号判断；引号内的 | 或 & 只是参数，不算管道或后台。
# 函数用途: 判断一段命令是否含有会让返回码不属于测试命令本身的管道或后台符号。
def _hides_exit_code(raw: str) -> bool:
    lexer = shlex.shlex(raw, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return any(token in _EXIT_CODE_HIDING_TOKENS for token in lexer)


# LLM: 一次 shell 返回码只能证明一条命令。只接受用 && 串起的段：出现 ;、||、管道、后台、空段或引号不成对时整条返回空列表，
#   调用方不得据此生成证据；多段时是否可信还要由调用方核对返回码是否为 0。开头的 cd 前缀已由 _cd_prefix 先行剥离。
# 函数用途: 把命令拆成按 && 串联的记号段，不满足条件时返回空列表。
def _command_segments(command: str) -> list[list[str]]:
    parts = _CHAIN_SPLIT_RE.split(command.strip())
    if any(operator != "&&" for operator in parts[1::2]):
        return []
    segments: list[list[str]] = []
    for raw in parts[0::2]:
        try:
            tokens = _tokens(raw)
        except ValueError:
            return []
        if not tokens or _hides_exit_code(raw):
            return []
        segments.append(tokens)
    return segments


def _tokens(value: str) -> list[str]:
    return [token.removeprefix("./") for token in shlex.split(value) if token]


def _strip_prefixes(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    if remaining and remaining[0] == "env":
        remaining = remaining[1:]
    while remaining and "=" in remaining[0] and not remaining[0].startswith("-"):
        remaining = remaining[1:]
    while remaining and remaining[0] in {"command", "time", "noglob"}:
        remaining = remaining[1:]
    return remaining


def _equivalent_spellings(needle: list[str]) -> tuple[list[str], ...]:
    variants = [needle]
    if len(needle) >= 3 and needle[1] == "run" and needle[0] in {"npm", "pnpm", "yarn", "bun"}:
        variants.append([needle[0], needle[2]])
    if needle == ["pytest"]:
        variants.extend([runner, "-m", "pytest"] for runner in ("python", "python3"))
        variants.extend([runner, "run", "pytest"] for runner in ("uv", "poetry", "pipenv"))
    if len(needle) == 1 and "/" in needle[0]:
        variants.extend([shell, needle[0]] for shell in ("bash", "sh"))
    return tuple(variants)


def _looks_like_target(arg: str) -> bool:
    return bool(
        arg
        and not arg.startswith("-")
        and "=" not in arg
        and (
            "/" in arg
            or "\\" in arg
            or "::" in arg
            or arg.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java"))
            or arg.startswith(("test_", "tests", "spec", "__tests__"))
        )
    )


def _kind_for_command(canonical: str) -> str:
    lowered = canonical.lower()
    if any(token in lowered for token in ("lint", "eslint", "ruff")):
        return "lint"
    if any(token in lowered for token in ("typecheck", "tsc", "mypy", "pyright", "ty")):
        return "typecheck"
    if "build" in lowered:
        return "build"
    if "fmt" in lowered or "format" in lowered:
        return "format"
    if "check" in lowered and "test" not in lowered:
        return "check"
    return "test"


def _summarize_output(output: str) -> str:
    text = str(output or "").strip()
    if len(text) <= _MAX_OUTPUT_SUMMARY_CHARS:
        return text
    head = _MAX_OUTPUT_SUMMARY_CHARS // 3
    tail = _MAX_OUTPUT_SUMMARY_CHARS - head
    return f"{text[:head]}\n... [{len(text) - _MAX_OUTPUT_SUMMARY_CHARS} chars omitted] ...\n{text[-tail:]}"


__all__ = [
    "ENVIRONMENT_UNAVAILABLE",
    "ClassifiedVerification",
    "ProjectFacts",
    "classify_verification_commands",
    "project_facts_for",
]
