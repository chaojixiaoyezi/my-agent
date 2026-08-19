from __future__ import annotations

"""Detect canonical project checks and classify commands without guessing intent.

LLM: this is the single verifier-command detector.  It reads manifest facts and
exact shell tokens; it never treats user prose or a command's output as authority.
模块用途: 找到代码项目根目录和项目已经声明的测试命令，用于区分局部与全量验证。
"""

import json
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
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;)\s*")
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


# LLM: only exact commands discovered from the current project can become
# evidence; a successful arbitrary shell command is not verification.
# 函数用途: 把一次真实命令结果归类成 targeted/full 验证证据。
def classify_verification_command(
    command: str,
    *,
    cwd: str | Path | None,
    exit_code: int,
    output: str,
) -> ClassifiedVerification | None:
    facts = project_facts_for(cwd)
    if facts is None or not facts.verify_commands or not isinstance(command, str) or not command.strip():
        return None
    match = _find_canonical_match(command, facts.verify_commands)
    if match is None:
        return None
    canonical, trailing_args = match
    return ClassifiedVerification(
        command=command,
        canonical_command=canonical,
        kind=_kind_for_command(canonical),
        scope="targeted" if any(_looks_like_target(arg) for arg in trailing_args) else "full",
        status="passed" if int(exit_code) == 0 else "failed",
        exit_code=int(exit_code),
        cwd=str(Path(cwd or ".").expanduser().resolve(strict=False)),
        root=str(facts.root),
        output_summary=_summarize_output(output),
    )


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


def _find_canonical_match(
    command: str,
    canonical_commands: tuple[str, ...],
) -> tuple[str, list[str]] | None:
    segments = _command_segments(command)
    for canonical in canonical_commands:
        needle = _tokens(canonical)
        for segment in segments:
            candidate = _strip_prefixes(segment)
            for spelling in _equivalent_spellings(needle):
                if candidate[: len(spelling)] == spelling:
                    return canonical, candidate[len(spelling) :]
    return None


def _command_segments(command: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for raw in _SHELL_SPLIT_RE.split(command.strip()):
        try:
            tokens = _tokens(raw)
        except ValueError:
            continue
        if tokens:
            segments.append(tokens)
    # One shell return code can only prove one command.  Chained commands can
    # hide an earlier failure (for example ``pytest; echo done``), so they are
    # intentionally not promoted to evidence.
    return segments if len(segments) == 1 else []


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
    "ClassifiedVerification",
    "ProjectFacts",
    "classify_verification_command",
    "project_facts_for",
]
