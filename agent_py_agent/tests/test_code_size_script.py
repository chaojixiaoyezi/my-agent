from __future__ import annotations

"""LLM: smoke tests for the code-size governance script.

给人看的解释：
这个测试保证代码规模检查脚本至少能在 warn 模式运行并生成报告。
"""

import ast
import subprocess
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
scripts_path = str(repo_root / "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

import check_code_size  # noqa: E402
from code_size_ast_checks import node_span  # noqa: E402
from code_size_docstrings import comment_line_numbers  # noqa: E402
from code_size_report import ReportRenderContext, write_report  # noqa: E402
from code_size_rules import Finding  # noqa: E402


def test_check_code_size_warn_generates_report() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_code_size.py", "--mode", "warn"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "CODE_SIZE_REPORT.md" in result.stdout
    assert (repo_root / "CODE_SIZE_REPORT.md").exists()


def test_near_soft_file_finding_is_high_risk(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    source = tmp_path / "agent_py_agent" / "near_soft_file.py"
    source.parent.mkdir()
    source.write_text("\n".join("pass" for _ in range(320)), encoding="utf-8")

    findings = check_code_size._check_file_size(source)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "file"
    assert finding.severity == "high-risk"
    assert finding.value == 320
    assert finding.limit == 400
    assert "near soft limit" in finding.message


def test_file_size_ignores_docstring_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    source = tmp_path / "agent_py_agent" / "documented_file.py"
    source.parent.mkdir()
    source.write_text('"""' + "\n".join(["LLM: docs"] * 330) + '"""\npass\n', encoding="utf-8")

    findings = check_code_size._check_file_size(source)

    assert findings == []


def test_file_size_ignores_full_line_comments(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    source = tmp_path / "agent_py_agent" / "commented_file.py"
    source.parent.mkdir()
    source.write_text("\n".join(["# LLM: docs"] * 330 + ["pass"]) + "\n", encoding="utf-8")

    findings = check_code_size._check_file_size(source)

    assert findings == []


def test_node_span_ignores_own_docstring() -> None:
    tree = ast.parse(
        "\n".join([
            "def documented():",
            '    """LLM: docs',
            "    函数用途: explain.",
            '    """',
            "    first = 1",
            "    return first",
        ])
    )
    node = tree.body[0]

    assert node_span(node) == 3


def test_node_span_ignores_full_line_comments() -> None:
    source = "\n".join(
        [
            "def documented():",
            "    # LLM: docs",
            "    # 函数用途: explain.",
            "    first = 1",
            "    return first",
        ]
    )
    tree = ast.parse(source)
    node = tree.body[0]

    assert node_span(node, comment_line_numbers(source)) == 3


def test_product_annotations_have_llm_and_human_purpose() -> None:
    problems = []
    for base in (repo_root / "agent_py_agent", repo_root / "scripts"):
        problems.extend(_annotation_coverage_problems(base))

    assert problems == []


def _annotation_coverage_problems(base: Path) -> list[str]:
    problems: list[str] = []
    for source in base.rglob("*.py"):
        rel = source.relative_to(repo_root).as_posix()
        if _skip_annotation_coverage(rel):
            continue
        text = source.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text, filename=rel)
        _collect_module_annotation_problems(lines, rel, problems)
        _collect_definition_annotation_problems(tree, lines, rel, problems)
    return problems


def _skip_annotation_coverage(rel: str) -> bool:
    return (
        "/tests/" in f"/{rel}"
        or rel.startswith("agent_py_agent/data/")
        or "__pycache__" in rel
    )


def _collect_module_annotation_problems(lines: list[str], rel: str, problems: list[str]) -> None:
    head = "\n".join(lines[:8])
    if "# LLM:" not in head or "# 模块用途:" not in head:
        problems.append(f"{rel}:1:<module>")


def _collect_definition_annotation_problems(tree: ast.AST, lines: list[str], rel: str, problems: list[str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        expected_human = "类用途:" if isinstance(node, ast.ClassDef) else "函数用途:"
        start = _definition_annotation_start(node)
        comments = _leading_comment_block(lines, start)
        if "# LLM:" not in comments or expected_human not in comments:
            problems.append(f"{rel}:{node.lineno}:{node.name}")


def _definition_annotation_start(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    decorator_lines = [decorator.lineno for decorator in node.decorator_list]
    return min(decorator_lines + [node.lineno])


def _leading_comment_block(lines: list[str], one_based_start: int) -> str:
    index = one_based_start - 2
    block: list[str] = []
    while index >= 0:
        line = lines[index]
        decision = _leading_comment_line_decision(line, has_block=bool(block))
        if decision == "stop":
            break
        if decision == "keep":
            block.append(line.strip())
        index -= 1
    return "\n".join(reversed(block))


def _leading_comment_line_decision(line: str, *, has_block: bool) -> str:
    stripped = line.strip()
    if stripped.startswith("#"):
        return "keep"
    if not stripped and not has_block:
        return "skip"
    return "stop"


def test_runtime_data_python_files_are_not_scanned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    runtime_file = tmp_path / "agent_py_agent" / "data" / "subagents" / "run-1" / "artifact.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text("def generated(a, b, c, d, e):\n    pass\n", encoding="utf-8")

    findings = check_code_size.collect_findings()

    assert findings == []


def test_near_soft_ast_findings_cover_requested_kinds(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    source = tmp_path / "agent_py_agent" / "near_soft_ast.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "def near_function(a, b, c, d, e):",
                *["    pass" for _ in range(47)],
                "",
                "def near_nesting():",
                "    if True:",
                "        if True:",
                "            if True:",
                "                pass",
                "",
                "class NearClass:",
                *["    pass" for _ in range(199)],
                "",
                "class NearMixin:",
                *["    pass" for _ in range(159)],
            ]
        ),
        encoding="utf-8",
    )

    findings = check_code_size._check_ast(source)
    by_kind = {(finding.kind, finding.name): finding for finding in findings}

    assert by_kind[("function", "near_function")].severity == "high-risk"
    assert by_kind[("params", "near_function")].severity == "high-risk"
    assert by_kind[("nesting", "near_nesting")].severity == "high-risk"
    assert by_kind[("class", "NearClass")].severity == "high-risk"
    assert by_kind[("mixin", "NearMixin")].severity == "high-risk"
    assert all("near soft limit" in finding.message for finding in by_kind.values())


def test_high_risk_findings_do_not_block_strict() -> None:
    findings = [
        Finding("function", "agent_py_agent/example.py", "near_function", 48, 60, "high-risk", "near soft limit"),
        Finding("file", "agent_py_agent/example.py", "example.py", 601, 600, "hard", "file too long"),
    ]

    blockers = check_code_size.compute_strict_blockers(findings, baseline=None)

    assert [finding.severity for finding in blockers] == ["hard"]


def test_report_surfaces_high_risk_near_soft_findings(tmp_path) -> None:
    report = tmp_path / "CODE_SIZE_REPORT.md"
    findings = [
        Finding(
            "function",
            "agent_py_agent/example.py",
            "near_function",
            48,
            60,
            "high-risk",
            "near soft limit: function approaching soft limit",
        )
    ]

    write_report(report, findings, ReportRenderContext("strict", False, None, False))

    text = report.read_text(encoding="utf-8")
    assert "- high_risk_findings: 1" in text
    assert "## 7. High-risk / near-soft Top 100" in text
    assert "| high-risk | function |" in text
    assert "- blocked: False" in text
