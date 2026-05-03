#!/usr/bin/env python3
from __future__ import annotations

"""LLM: code-size governance checker for files, functions, classes, and imports.

给人看的解释：
这个脚本先以 warn 模式暴露历史技术债，并生成 CODE_SIZE_REPORT.md。
strict 模式用于后续 CI 收紧，阻断新增违规。
支持 baseline 机制：历史违规不阻断，新增/恶化的违规阻断。
"""

import argparse
import ast
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ["agent_py_agent", "scripts"]
REPORT_PATH = ROOT / "CODE_SIZE_REPORT.md"

FILE_SOFT_LIMIT = 400
FILE_HARD_LIMIT = 600
TEST_SOFT_LIMIT = 700
TEST_HARD_LIMIT = 900
FUNCTION_SOFT_LIMIT = 60
FUNCTION_HARD_LIMIT = 100
CLASS_SOFT_LIMIT = 250
CLASS_HARD_LIMIT = 350
MIXIN_SOFT_LIMIT = 200
MIXIN_HARD_LIMIT = 250
PARAM_SOFT_LIMIT = 6
PARAM_HARD_LIMIT = 8
NESTING_SOFT_LIMIT = 3
NESTING_HARD_LIMIT = 4

JUNK_NAMES = {
    "common.py",
    "final.py",
    "final2.py",
    "helper.py",
    "helpers.py",
    "manager2.py",
    "manager_extra.py",
    "misc.py",
    "new.py",
    "old.py",
    "temp.py",
    "tmp.py",
    "utils.py",
}
JUNK_NAME_BASELINE = {
    "agent_py_agent/agent/log_analysis/analytics/detectors/helpers.py",
    "agent_py_agent/agent/log_analysis/parsers/common.py",
    "agent_py_agent/agent/subagents/utils.py",
    "agent_py_agent/cli/common.py",
}

# High-risk files frozen by architecture guardrails.
HIGH_RISK_FILES: dict[str, int] = {
    "agent_py_agent/cli/chat.py": 1017,
    "agent_py_agent/agent/agent_core/dispatch_mixin.py": 895,
    "agent_py_agent/agent/memory_archive/query.py": 839,
    "agent_py_agent/agent/subagents/manager_patch.py": 794,
    "agent_py_agent/agent/settings/config.py": 751,
    "agent_py_agent/agent/subagents/manager_base.py": 751,
    "agent_py_agent/agent/log_analysis/analytics/detectors/rules.py": 747,
    "agent_py_agent/agent/log_analysis/tools.py": 672,
    "agent_py_agent/agent/memory_archive/runtime.py": 657,
    "agent_py_agent/agent/adapter/qq.py": 613,
    "agent_py_agent/cli/memory_commands.py": 609,
}

# Patterns to exclude from scanning
EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "htmlcov",
    ".coverage",
}
EXCLUDE_PREFIXES = ("._",)
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_NAMES = {".DS_Store", ".AppleDouble", ".LSOverride"}


@dataclass
class Finding:
    kind: str
    path: str
    name: str
    value: int
    limit: int
    severity: str
    message: str

    def identity(self) -> str:
        return f"{self.kind}:{self.path}:{self.name}"


def _is_excluded(path: Path) -> bool:
    """Check if a path should be excluded from scanning."""
    for part in path.parts:
        if part in EXCLUDE_PARTS:
            return True
        if part in EXCLUDE_NAMES:
            return True
        if any(part.startswith(p) for p in EXCLUDE_PREFIXES):
            return True
        if any(part.endswith(s) for s in EXCLUDE_SUFFIXES):
            return True
    return False


def _git_added_files() -> set[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    added: set[str] = set()
    for line in result.stdout.splitlines():
        if line.startswith("?? "):
            added.add(line[3:].rstrip("/"))
        elif line[:2] in {"A ", "AM", "??"}:
            added.add(line[3:])
    return added


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SOURCE_ROOTS:
        root = ROOT / root_name
        if root.is_file() and root.suffix == ".py":
            if not _is_excluded(root):
                files.append(root)
        elif root.exists():
            files.extend(
                path
                for path in root.rglob("*.py")
                if not _is_excluded(path)
            )
    return sorted(files)


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _node_span(node: ast.AST) -> int:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start)
    return max(0, end - start + 1)


def _arg_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    args = node.args
    return (
        len(args.posonlyargs)
        + len(args.args)
        + len(args.kwonlyargs)
        + (1 if args.vararg else 0)
        + (1 if args.kwarg else 0)
    )


def _max_nesting(node: ast.AST) -> int:
    branch_nodes = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)

    def walk(current: ast.AST, depth: int) -> int:
        next_depth = depth + 1 if isinstance(current, branch_nodes) else depth
        child_depths = [walk(child, next_depth) for child in ast.iter_child_nodes(current)]
        return max([next_depth, *child_depths])

    return walk(node, 0)


def _read_text_safe(path: Path) -> str | None:
    """Read a file as UTF-8, returning None on decode error."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _check_file_size(path: Path) -> list[Finding]:
    rel = _relative(path)
    text = _read_text_safe(path)
    if text is None:
        return [Finding("decode_error", rel, path.name, 0, 0, "hard", "failed to decode source file as UTF-8")]
    line_count = len(text.splitlines())
    is_test = "/tests/" in f"/{rel}" or rel.startswith("test")
    soft = TEST_SOFT_LIMIT if is_test else FILE_SOFT_LIMIT
    hard = TEST_HARD_LIMIT if is_test else FILE_HARD_LIMIT
    if line_count <= soft:
        return []
    severity = "hard" if line_count > hard else "soft"
    limit = hard if severity == "hard" else soft
    return [
        Finding(
            "file",
            rel,
            path.name,
            line_count,
            limit,
            severity,
            f"{rel} has {line_count} lines",
        )
    ]


def _check_ast(path: Path) -> list[Finding]:
    rel = _relative(path)
    findings: list[Finding] = []
    text = _read_text_safe(path)
    if text is None:
        return [Finding("decode_error", rel, path.name, 0, 0, "hard", "failed to decode source file as UTF-8")]
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        return [Finding("syntax", rel, path.name, exc.lineno or 0, 0, "hard", str(exc))]

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            findings.append(Finding("import_star", rel, "*", 1, 0, "hard", "star import is forbidden"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = _node_span(node)
            if span > FUNCTION_SOFT_LIMIT:
                severity = "hard" if span > FUNCTION_HARD_LIMIT else "soft"
                findings.append(Finding("function", rel, node.name, span, FUNCTION_HARD_LIMIT, severity, "function too long"))
            params = _arg_count(node)
            if params > PARAM_SOFT_LIMIT:
                severity = "hard" if params > PARAM_HARD_LIMIT else "soft"
                findings.append(Finding("params", rel, node.name, params, PARAM_HARD_LIMIT, severity, "too many parameters"))
            nesting = _max_nesting(node)
            if nesting > NESTING_SOFT_LIMIT:
                severity = "hard" if nesting > NESTING_HARD_LIMIT else "soft"
                findings.append(Finding("nesting", rel, node.name, nesting, NESTING_HARD_LIMIT, severity, "nesting too deep"))
        if isinstance(node, ast.ClassDef):
            span = _node_span(node)
            soft = MIXIN_SOFT_LIMIT if node.name.endswith("Mixin") else CLASS_SOFT_LIMIT
            hard = MIXIN_HARD_LIMIT if node.name.endswith("Mixin") else CLASS_HARD_LIMIT
            if span > soft:
                severity = "hard" if span > hard else "soft"
                findings.append(Finding("class", rel, node.name, span, hard, severity, "class too long"))
    return findings


def _check_junk_names(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    added = _git_added_files()
    for path in paths:
        rel = _relative(path)
        if path.name not in JUNK_NAMES or rel in JUNK_NAME_BASELINE:
            continue
        severity = "hard" if any(rel == item or rel.startswith(f"{item}/") for item in added) else "soft"
        findings.append(Finding("junk_name", rel, path.name, 1, 0, severity, "vague filename"))
    return findings


def _check_high_risk_files() -> list[Finding]:
    """Check that frozen high-risk files have not grown past their baseline."""
    findings: list[Finding] = []
    for rel_path, baseline in HIGH_RISK_FILES.items():
        path = ROOT / rel_path
        if not path.exists():
            continue
        text = _read_text_safe(path)
        if text is None:
            continue
        current = len(text.splitlines())
        if current > baseline:
            findings.append(
                Finding(
                    "high_risk_growth",
                    rel_path,
                    path.name,
                    current,
                    baseline,
                    "hard",
                    f"{rel_path} grew from {baseline} to {current} lines",
                )
            )
    return findings


def collect_findings() -> list[Finding]:
    files = _source_files()
    findings: list[Finding] = []
    for path in files:
        findings.extend(_check_file_size(path))
        findings.extend(_check_ast(path))
    findings.extend(_check_junk_names(files))
    findings.extend(_check_high_risk_files())
    return sorted(findings, key=lambda item: (item.severity != "hard", item.kind, item.path, item.name))


def load_baseline(baseline_path: Path) -> dict[str, str]:
    """Load baseline file mapping finding identity -> severity."""
    if not baseline_path.exists():
        return {}
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    return {item["identity"]: item["severity"] for item in data.get("findings", [])}


def write_baseline(findings: list[Finding], baseline_path: Path) -> None:
    """Write current findings as baseline."""
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_findings": len(findings),
        "findings": [
            {"identity": f.identity(), "severity": f.severity, "kind": f.kind, "path": f.path, "name": f.name}
            for f in findings
        ],
    }
    baseline_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def compute_strict_blockers(findings: list[Finding], baseline: dict[str, str] | None) -> list[Finding]:
    """Compute which findings should block in strict mode.

    Without baseline: all hard findings block.
    With baseline: only new or worsened hard findings block.
    """
    blockers: list[Finding] = []
    for f in findings:
        if f.severity != "hard":
            continue
        if baseline is None:
            blockers.append(f)
            continue
        # With baseline: block if finding is new (not in baseline)
        # or if it's high_risk_growth (always blocks)
        fid = f.identity()
        if fid not in baseline or f.kind == "high_risk_growth":
            blockers.append(f)
    return blockers


def _format_table(findings: list[Finding], limit: int = 0) -> list[str]:
    if not findings:
        return ["- none"]
    lines = ["| Severity | Kind | Path | Name | Value | Limit | Message |", "| --- | --- | --- | --- | ---: | ---: | --- |"]
    items = findings[:limit] if limit > 0 else findings
    for item in items:
        lines.append(
            f"| {item.severity} | {item.kind} | `{item.path}` | `{item.name}` | "
            f"{item.value} | {item.limit} | {item.message} |"
        )
    if limit > 0 and len(findings) > limit:
        lines.append(f"| ... | ... | ... | ... | ... | ... | *{len(findings) - limit} more* |")
    return lines


def write_report(
    findings: list[Finding],
    *,
    mode: str,
    blocked: bool,
    baseline_path: str | None,
    baseline_loaded: bool,
) -> None:
    hard = [item for item in findings if item.severity == "hard"]
    soft = [item for item in findings if item.severity != "hard"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# CODE SIZE REPORT",
        "",
        f"Generated at: {now}",
        f"Generated by: `python scripts/check_code_size.py --mode {mode}`",
        "",
        f"- mode: {mode}",
        f"- baseline: {baseline_path or 'none'}",
        f"- baseline_loaded: {baseline_loaded}",
        f"- blocked: {blocked}",
        f"- total_findings: {len(findings)}",
        f"- hard_findings: {len(hard)}",
        f"- soft_findings: {len(soft)}",
        "",
        "## 1. 超长文件 Top 20",
        *_format_table([item for item in findings if item.kind == "file"], 20),
        "",
        "## 2. 超长函数 Top 20",
        *_format_table([item for item in findings if item.kind == "function"], 20),
        "",
        "## 3. 超长类 Top 20",
        *_format_table([item for item in findings if item.kind == "class"], 20),
        "",
        "## 4. 高复杂度函数 Top 20",
        *_format_table([item for item in findings if item.kind == "nesting"], 20),
        "",
        "## 5. 高危文件增长",
        *_format_table([item for item in findings if item.kind == "high_risk_growth"]),
        "",
        "## 6. import * 违规",
        *_format_table([item for item in findings if item.kind == "import_star"]),
        "",
        "## 7. decode error 违规",
        *_format_table([item for item in findings if item.kind == "decode_error"]),
        "",
        "## 8. junk file / junk name 违规",
        *_format_table([item for item in findings if item.kind == "junk_name"]),
        "",
        "## 9. 历史遗留项 (soft)",
        *_format_table(soft[:100]),
        "",
        "## 10. 下一步建议",
        "- Keep `cli/parser.py` thin and route registration through `cli/commands/`.",
        "- Continue extracting `cli/chat.py` into chat session, input loop, renderer, and gateway client modules.",
        "- Move SubAgent mixin logic into services and repositories behind the manager facade.",
        "- Split memory archive query/runtime and log analysis tools by query, rendering, and persistence responsibilities.",
        "- Run `--write-baseline` to capture current state, then use `--mode strict --baseline` to block only new violations.",
        "",
        "## 11. 本次是否阻断",
        f"- {'**yes**' if blocked else 'no'}",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check code-size engineering guardrails.")
    parser.add_argument("--mode", choices=["warn", "strict"], default="warn")
    parser.add_argument("--baseline", type=str, default=None, help="Path to baseline JSON file")
    parser.add_argument("--write-baseline", type=str, default=None, help="Write current findings as baseline")
    args = parser.parse_args()

    findings = collect_findings()

    # Write baseline if requested
    if args.write_baseline:
        write_baseline(findings, Path(args.write_baseline))
        print(f"baseline written to {args.write_baseline}")

    # Load baseline if provided
    baseline: dict[str, str] | None = None
    baseline_loaded = False
    if args.baseline:
        bp = Path(args.baseline)
        if bp.exists():
            baseline = load_baseline(bp)
            baseline_loaded = True
        else:
            print(f"WARNING: baseline file not found: {args.baseline}", file=sys.stderr)

    blockers = compute_strict_blockers(findings, baseline)
    blocked = args.mode == "strict" and bool(blockers)

    write_report(findings, mode=args.mode, blocked=blocked, baseline_path=args.baseline, baseline_loaded=baseline_loaded)

    print(f"code-size findings: total={len(findings)} hard={len([f for f in findings if f.severity == 'hard'])} report={REPORT_PATH.relative_to(ROOT)} blocked={blocked}")

    if blocked:
        for item in blockers:
            print(f"BLOCKED: {item.severity}: {item.kind}: {item.path}:{item.name} {item.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
