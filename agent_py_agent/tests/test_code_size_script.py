from __future__ import annotations

"""Smoke tests for the code-size governance script."""

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


def test_large_file_size_does_not_create_a_finding(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(check_code_size, "ROOT", tmp_path)
    source = tmp_path / "agent_py_agent" / "large_file.py"
    source.parent.mkdir()
    source.write_text("\n".join("pass" for _ in range(900)), encoding="utf-8")

    findings = check_code_size.collect_findings()

    assert findings == []


def test_node_span_ignores_own_docstring() -> None:
    tree = ast.parse(
        "\n".join([
            "def documented():",
            '    """Explain why this helper exists.',
            "    It is intentionally verbose.",
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
            "    # Explain why this helper exists.",
            "    first = 1",
            "    return first",
        ]
    )
    tree = ast.parse(source)
    node = tree.body[0]

    assert node_span(node, comment_line_numbers(source)) == 3


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


def test_legacy_file_size_findings_do_not_block_strict() -> None:
    findings = [
        Finding("function", "agent_py_agent/example.py", "near_function", 48, 60, "high-risk", "near soft limit"),
        Finding("file", "agent_py_agent/example.py", "example.py", 601, 600, "hard", "file too long"),
    ]

    blockers = check_code_size.compute_strict_blockers(findings, baseline=None)

    assert blockers == []


def test_non_file_hard_findings_still_block_strict() -> None:
    findings = [
        Finding("function", "agent_py_agent/example.py", "too_long", 120, 100, "hard", "function too long"),
        Finding("file", "agent_py_agent/example.py", "example.py", 2000, 600, "hard", "file too long"),
    ]

    blockers = check_code_size.compute_strict_blockers(findings, baseline=None)

    assert [(finding.kind, finding.name) for finding in blockers] == [("function", "too_long")]


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
    assert "## 6. High-risk / near-soft Top 100" in text
    assert "| high-risk | function |" in text
    assert "- blocked: False" in text
