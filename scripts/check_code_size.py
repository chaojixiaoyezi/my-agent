#!/usr/bin/env python3
from __future__ import annotations

"""LLM: code-size governance checker for files, functions, classes, and imports.

给人看的解释：
这个脚本先以 warn 模式暴露历史技术债，并生成 CODE_SIZE_REPORT.md。
strict 模式用于后续 CI 收紧，阻断新增星号导入、垃圾命名和新增超硬上限文件。
"""

import argparse
import ast
import subprocess
from dataclasses import dataclass
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
# These files must NOT grow; new code goes to extracted modules.
HIGH_RISK_FILES: dict[str, int] = {
    "agent_py_agent/cli/chat.py": 989,
    "agent_py_agent/agent/agent_core/dispatch_mixin.py": 889,
    "agent_py_agent/agent/memory_archive/query.py": 839,
    "agent_py_agent/agent/subagents/manager_patch.py": 794,
    "agent_py_agent/agent/settings/config.py": 751,
    "agent_py_agent/agent/subagents/manager_base.py": 744,
    "agent_py_agent/agent/log_analysis/analytics/detectors/rules.py": 747,
    "agent_py_agent/agent/log_analysis/tools.py": 666,
    "agent_py_agent/agent/memory_archive/runtime.py": 657,
    "agent_py_agent/agent/adapter/qq.py": 613,
    "agent_py_agent/cli/memory_commands.py": 609,
}


@dataclass
class Finding:
    kind: str
    path: str
    name: str
    value: int
    limit: int
    severity: str
    message: str


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
            files.append(root)
        elif root.exists():
            files.extend(path for path in root.rglob("*.py") if ".git" not in path.parts)
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


def _check_file_size(path: Path) -> list[Finding]:
    rel = _relative(path)
    line_count = len(path.read_text(encoding="utf-8").splitlines())
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
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
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
        current = len(path.read_text(encoding="utf-8").splitlines())
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


def _format_table(findings: list[Finding]) -> list[str]:
    if not findings:
        return ["- none"]
    lines = ["| Severity | Kind | Path | Name | Value | Limit | Message |", "| --- | --- | --- | --- | ---: | ---: | --- |"]
    for item in findings:
        lines.append(
            f"| {item.severity} | {item.kind} | `{item.path}` | `{item.name}` | "
            f"{item.value} | {item.limit} | {item.message} |"
        )
    return lines


def write_report(findings: list[Finding], *, mode: str, blocked: bool) -> None:
    hard = [item for item in findings if item.severity == "hard"]
    soft = [item for item in findings if item.severity != "hard"]
    lines = [
        "# CODE SIZE REPORT",
        "",
        "LLM: Generated by `python scripts/check_code_size.py`.",
        "",
        "给人看的解释：",
        "这份报告列出代码规模、函数长度、类长度、参数数量、嵌套深度和命名风险。",
        "",
        f"- mode: {mode}",
        f"- blocked: {blocked}",
        f"- total_findings: {len(findings)}",
        f"- hard_findings: {len(hard)}",
        f"- soft_findings: {len(soft)}",
        "",
        "## 1. 超长文件列表",
        *_format_table([item for item in findings if item.kind == "file"]),
        "",
        "## 2. 超长函数列表",
        *_format_table([item for item in findings if item.kind == "function"]),
        "",
        "## 3. 超长类列表",
        *_format_table([item for item in findings if item.kind == "class"]),
        "",
        "## 4. 高复杂度函数列表",
        *_format_table([item for item in findings if item.kind == "nesting"]),
        "",
        "## 5. 高危文件增长",
        *_format_table([item for item in findings if item.kind == "high_risk_growth"]),

        "## 6. 新增违规项",
        *_format_table([item for item in hard if item.kind in {"import_star", "junk_name", "syntax"}]),
        "",
        "## 7. 历史遗留项",
        *_format_table(soft[:100]),
        "",
        "## 8. 建议拆分路径",
        "- Keep `cli/parser.py` thin and route registration through `cli/commands/`.",
        "- Continue extracting `cli/chat.py` into chat session, input loop, renderer, and gateway client modules.",
        "- Move SubAgent mixin logic into services and repositories behind the manager facade.",
        "- Split memory archive query/runtime and log analysis tools by query, rendering, and persistence responsibilities.",
        "",
        "## 9. 本次是否阻断",
        f"- {'yes' if blocked else 'no'}",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check code-size engineering guardrails.")
    parser.add_argument("--mode", choices=["warn", "strict"], default="warn")
    args = parser.parse_args()
    findings = collect_findings()
    strict_blockers = [item for item in findings if item.severity == "hard" and item.kind in {"import_star", "junk_name", "syntax", "high_risk_growth"}]
    blocked = args.mode == "strict" and bool(strict_blockers)
    write_report(findings, mode=args.mode, blocked=blocked)
    print(f"code-size findings: total={len(findings)} report={REPORT_PATH.relative_to(ROOT)} blocked={blocked}")
    if blocked:
        for item in strict_blockers:
            print(f"{item.severity}: {item.kind}: {item.path}:{item.name} {item.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
