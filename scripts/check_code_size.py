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
from datetime import datetime, timezone
from pathlib import Path

from code_size_ast_checks import check_class_node, check_function_node
from code_size_report import ReportRenderContext, write_report
from code_size_rules import (
    EXCLUDE_NAMES,
    EXCLUDE_PARTS,
    EXCLUDE_PREFIXES,
    EXCLUDE_SUFFIXES,
    FILE_HARD_LIMIT,
    FILE_SOFT_LIMIT,
    HIGH_RISK_FILES,
    JUNK_NAME_BASELINE,
    JUNK_NAMES,
    SOURCE_ROOTS,
    TEST_HARD_LIMIT,
    TEST_SOFT_LIMIT,
    Finding,
)
from code_size_thresholds import (
    FindingInput,
    LimitFindingInput,
    is_near_soft,
    limit_finding,
    near_soft_finding,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "CODE_SIZE_REPORT.md"


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
        item = _added_file_from_status_line(line)
        if item:
            added.add(item)
    return added


def _added_file_from_status_line(line: str) -> str:
    if line.startswith("?? "):
        return line[3:].rstrip("/")
    if line[:2] in {"A ", "AM", "??"}:
        return line[3:]
    return ""


def _source_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SOURCE_ROOTS:
        files.extend(_source_files_under(ROOT / root_name))
    return sorted(files)


def _source_files_under(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".py":
        return [] if _is_excluded(root) else [root]
    if not root.exists():
        return []
    return [path for path in root.rglob("*.py") if not _is_excluded(path)]


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


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
        if is_near_soft(line_count, soft):
            return [
                near_soft_finding(
                    FindingInput("file", rel, path.name, line_count, soft, f"{rel} has {line_count} lines")
                )
            ]
        return []
    return [limit_finding(LimitFindingInput(FindingInput("file", rel, path.name, line_count, soft, f"{rel} has {line_count} lines"), hard))]


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
            findings.extend(check_function_node(rel, node))
        if isinstance(node, ast.ClassDef):
            findings.extend(check_class_node(rel, node))
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check code-size engineering guardrails.")
    parser.add_argument("--mode", choices=["warn", "strict"], default="warn")
    parser.add_argument("--baseline", type=str, default=None, help="Path to baseline JSON file")
    parser.add_argument("--write-baseline", type=str, default=None, help="Write current findings as baseline")
    return parser.parse_args()


def _load_optional_baseline(path: str | None) -> tuple[dict[str, str] | None, bool]:
    if not path:
        return None, False
    baseline_path = Path(path)
    if baseline_path.exists():
        return load_baseline(baseline_path), True
    print(f"WARNING: baseline file not found: {path}", file=sys.stderr)
    return None, False


def _write_requested_baseline(findings: list[Finding], path: str | None) -> None:
    if not path:
        return
    write_baseline(findings, Path(path))
    print(f"baseline written to {path}")


def _print_summary(findings: list[Finding], blocked: bool) -> None:
    hard_count = len([f for f in findings if f.severity == "hard"])
    high_risk_count = len([f for f in findings if f.severity == "high-risk"])
    soft_count = len([f for f in findings if f.severity == "soft"])
    print(
        "code-size findings: "
        f"total={len(findings)} hard={hard_count} high-risk={high_risk_count} "
        f"soft={soft_count} report={REPORT_PATH.relative_to(ROOT)} blocked={blocked}"
    )


def main() -> int:
    args = _parse_args()
    findings = collect_findings()
    _write_requested_baseline(findings, args.write_baseline)
    baseline, baseline_loaded = _load_optional_baseline(args.baseline)
    blockers = compute_strict_blockers(findings, baseline)
    blocked = args.mode == "strict" and bool(blockers)
    context = ReportRenderContext(args.mode, blocked, args.baseline, baseline_loaded)
    write_report(REPORT_PATH, findings, context)
    _print_summary(findings, blocked)
    if blocked:
        for item in blockers:
            print(f"BLOCKED: {item.severity}: {item.kind}: {item.path}:{item.name} {item.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
