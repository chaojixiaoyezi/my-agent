#!/usr/bin/env python3
# LLM: Code-size governance helper; keep report identities, thresholds, and baseline behavior stable.
# 模块用途: 支撑代码规模守卫，统计文件/函数/类大小并生成可审查的报告。

from __future__ import annotations

"""code-size governance checker for files, functions, classes, and imports.

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
from code_size_docstrings import non_code_line_numbers
from code_size_report import ReportRenderContext, write_report
from code_size_rules import (
    EXCLUDE_NAMES,
    EXCLUDE_PARTS,
    EXCLUDE_PATH_PREFIXES,
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
DEFAULT_BASELINE_PATH = ROOT / "CODE_SIZE_BASELINE.json"
_SEVERITY_RANK = {"soft": 1, "high-risk": 2, "hard": 3}


# LLM: _is_excluded 是扫描入口的路径闸门；目录白名单变化会影响所有规模检查。
# 函数用途: 判断路径是否属于缓存、生成物、运行时数据或第三方产物。
def _is_excluded(path: Path) -> bool:
    """Check if a path should be excluded from scanning."""
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    if any(rel.startswith(prefix) for prefix in EXCLUDE_PATH_PREFIXES):
        return True
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


# LLM: _git_added_files 只服务新增文件命名守卫；失败时保持非阻断。
# 函数用途: 读取 git status，把新增文件路径整理成仓库相对路径集合。
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


# LLM: _added_file_from_status_line 解析 porcelain 行；保守返回能避免误阻断。
# 函数用途: 从单行 git status 输出中识别新增文件，无法识别时返回空字符串。
def _added_file_from_status_line(line: str) -> str:
    if line.startswith("?? "):
        return line[3:].rstrip("/")
    if line[:2] in {"A ", "AM", "??"}:
        return line[3:]
    return ""


# LLM: _source_files 汇总规模检查入口；SOURCE_ROOTS 变化会影响报告覆盖面。
# 函数用途: 展开所有源码根，返回参与文件大小、AST 和命名检查的 Python 文件。
def _source_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SOURCE_ROOTS:
        files.extend(_source_files_under(ROOT / root_name))
    return sorted(files)


# LLM: _source_files_under 复用统一排除策略；不要绕过 _is_excluded。
# 函数用途: 遍历一个源码根下的 Python 文件，并跳过生成物和运行时目录。
def _source_files_under(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".py":
        return [] if _is_excluded(root) else [root]
    if not root.exists():
        return []
    return [path for path in root.rglob("*.py") if not _is_excluded(path)]


# LLM: _relative 生成报告身份；baseline 和本地路径都依赖这个口径。
# 函数用途: 将绝对路径转成仓库相对路径，保证不同机器上的报告稳定。
def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# LLM: _read_text_safe 是解析前的 UTF-8 入口；调用方把 None 转成 finding。
# 函数用途: 读取源码文本，遇到解码失败时返回 None 而不是抛出异常。
def _read_text_safe(path: Path) -> str | None:
    """Read a file as UTF-8, returning None on decode error."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


# LLM: _effective_file_line_count 定义文件规模口径；注释和 docstring 不算实现行。
# 函数用途: 计算有效代码行数，避免维护注释触发文件过大告警。
def _effective_file_line_count(text: str) -> int:
    physical = len(text.splitlines())
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return physical
    return max(0, physical - len(non_code_line_numbers(text, tree)))


# LLM: _check_file_size 产生文件级 finding；阈值口径需和报告说明一致。
# 函数用途: 检查单个文件是否接近或超过大小限制，并生成对应 finding。
def _check_file_size(path: Path) -> list[Finding]:
    rel = _relative(path)
    text = _read_text_safe(path)
    if text is None:
        return [Finding("decode_error", rel, path.name, 0, 0, "hard", "failed to decode source file as UTF-8")]
    line_count = _effective_file_line_count(text)
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


# LLM: _check_ast 汇总 AST 级规则；语法错误、星号导入和节点规模都在这里归口。
# 函数用途: 解析源码 AST，收集函数、类、嵌套、参数和 import-star findings。
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
    ignored_lines = non_code_line_numbers(text, tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            findings.append(Finding("import_star", rel, "*", 1, 0, "hard", "star import is forbidden"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.extend(check_function_node(rel, node, ignored_lines))
        if isinstance(node, ast.ClassDef):
            findings.extend(check_class_node(rel, node, ignored_lines))
    return findings


# LLM: _check_junk_names 保护新增文件命名；历史债只保留 soft 提醒。
# 函数用途: 检查模糊文件名，并只对新增违规文件产生 hard finding。
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


# LLM: _check_high_risk_files 守住冻结文件基线；增长时直接 strict 阻断。
# 函数用途: 对比高风险文件当前行数和冻结 baseline，报告新增膨胀。
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


# LLM: collect_findings 是 code-size 总入口；报告和 strict 模式共用这份排序结果。
# 函数用途: 运行文件、AST、命名和高风险检查，并按稳定顺序返回 findings。
def collect_findings() -> list[Finding]:
    files = _source_files()
    findings: list[Finding] = []
    for path in files:
        findings.extend(_check_file_size(path))
        findings.extend(_check_ast(path))
    findings.extend(_check_junk_names(files))
    findings.extend(_check_high_risk_files())
    return sorted(findings, key=lambda item: (item.severity != "hard", item.kind, item.path, item.name))


# LLM: load_baseline 读取 strict 豁免口径；JSON 字段需要向后兼容。
# 函数用途: 从 baseline JSON 中加载 finding identity 到 severity 的映射。
def load_baseline(baseline_path: Path) -> dict[str, str]:
    """Load baseline file mapping finding identity -> severity."""
    if not baseline_path.exists():
        return {}
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    return {item["identity"]: item["severity"] for item in data.get("findings", [])}


# LLM: write_baseline 写出当前豁免快照；字段变化会影响 CI 收紧流程。
# 函数用途: 将当前 findings 序列化为 baseline JSON，供后续 strict 比对。
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


# LLM: compute_strict_blockers 决定 strict 退出码；新增 hard finding 不能漏掉。
# 函数用途: 根据 baseline 判断哪些 hard findings 应该阻断本次检查。
def compute_strict_blockers(findings: list[Finding], baseline: dict[str, str] | None) -> list[Finding]:
    """Compute which findings should block in strict mode.

    Without baseline: all hard findings block.
    With baseline: only new or worsened hard findings block."""
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


# LLM: report_findings filters historical baseline debt so CODE_SIZE_REPORT represents current net risk.
# 函数用途: 生成报告时只展示新增、恶化或强制阻断的规模问题，避免下游矩阵把历史 baseline 当作新失败。
def report_findings(findings: list[Finding], baseline: dict[str, str] | None) -> list[Finding]:
    if baseline is None:
        return findings
    return [item for item in findings if _finding_exceeds_baseline(item, baseline)]


def _finding_exceeds_baseline(item: Finding, baseline: dict[str, str]) -> bool:
    if item.kind == "high_risk_growth":
        return True
    previous = baseline.get(item.identity())
    if previous is None:
        return True
    return _SEVERITY_RANK.get(item.severity, 0) > _SEVERITY_RANK.get(previous, 0)


# LLM: _parse_args 定义脚本参数面；改参数会影响 CI 调用方式。
# 函数用途: 注册 warn/strict、baseline 和写 baseline 等命令行参数。
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check code-size engineering guardrails.")
    parser.add_argument("--mode", choices=["warn", "strict"], default="warn")
    parser.add_argument("--baseline", type=str, default=None, help="Path to baseline JSON file")
    parser.add_argument("--write-baseline", type=str, default=None, help="Write current findings as baseline")
    return parser.parse_args()


# LLM: _load_optional_baseline 处理 baseline 缺失；警告文案会进入 CI 日志。
# 函数用途: 在用户提供路径时加载 baseline，缺失时返回未加载状态。
def _load_optional_baseline(path: str | None) -> tuple[dict[str, str] | None, bool]:
    if not path:
        return None, False
    baseline_path = Path(path)
    if baseline_path.exists():
        return load_baseline(baseline_path), True
    print(f"WARNING: baseline file not found: {path}", file=sys.stderr)
    return None, False


# LLM: _effective_baseline_arg gives local checks the repo baseline unless a caller explicitly overrides it.
# 函数用途: 让 warn/strict 生成同一净报告口径；没有默认 baseline 时保持旧的无 baseline 行为。
def _effective_baseline_arg(path: str | None) -> str | None:
    if path:
        return path
    if DEFAULT_BASELINE_PATH.exists():
        return DEFAULT_BASELINE_PATH.relative_to(ROOT).as_posix()
    return None


# LLM: _write_requested_baseline 只在显式请求时写文件；默认检查不落盘。
# 函数用途: 根据 --write-baseline 写出当前 findings 并打印生成位置。
def _write_requested_baseline(findings: list[Finding], path: str | None) -> None:
    if not path:
        return
    write_baseline(findings, Path(path))
    print(f"baseline written to {path}")


# LLM: _print_summary 是脚本的单行结果摘要；日志解析依赖字段名。
# 函数用途: 输出 findings 总数、严重级别计数、报告路径和阻断状态。
def _print_summary(findings: list[Finding], blocked: bool) -> None:
    hard_count = len([f for f in findings if f.severity == "hard"])
    high_risk_count = len([f for f in findings if f.severity == "high-risk"])
    soft_count = len([f for f in findings if f.severity == "soft"])
    print(
        "code-size findings: "
        f"total={len(findings)} hard={hard_count} high-risk={high_risk_count} "
        f"soft={soft_count} report={REPORT_PATH.relative_to(ROOT)} blocked={blocked}"
    )


# LLM: main 编排 code-size 检查、baseline、报告和退出码；CI 直接调用它。
# 函数用途: 解析参数，收集 findings，写报告，并在 strict 阻断时返回 1。
def main() -> int:
    args = _parse_args()
    findings = collect_findings()
    _write_requested_baseline(findings, args.write_baseline)
    baseline_arg = _effective_baseline_arg(args.baseline)
    baseline, baseline_loaded = _load_optional_baseline(baseline_arg)
    blockers = compute_strict_blockers(findings, baseline)
    blocked = args.mode == "strict" and bool(blockers)
    visible_findings = report_findings(findings, baseline)
    context = ReportRenderContext(args.mode, blocked, baseline_arg, baseline_loaded)
    write_report(REPORT_PATH, visible_findings, context)
    _print_summary(visible_findings, blocked)
    if blocked:
        for item in blockers:
            print(f"BLOCKED: {item.severity}: {item.kind}: {item.path}:{item.name} {item.message}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
