from __future__ import annotations

from pathlib import Path


def test_offline_contract_matrix_gate_passes_current_repo() -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix, main

    repo_root = Path(__file__).resolve().parents[2]

    report = check_offline_contract_matrix(repo_root)

    assert report.ok is True
    assert report.missing_areas == ()
    assert report.code_size_blocked is False
    assert main(["--repo-root", str(repo_root), "--json"]) == 0


def test_offline_contract_matrix_gate_reports_missing_files(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    (tmp_path / "CODE_SIZE_REPORT.md").write_text(
        "- blocked: False\n"
        "- strict_scope_high_risk_findings: 0\n"
        "- strict_scope_soft_findings: 0\n"
        "- test_advisory_findings: 9\n",
        encoding="utf-8",
    )

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is False
    assert "concurrency" in report.missing_areas
    assert report.findings[0]["code"] == "OFFLINE_MATRIX_FILE_MISSING"


def test_offline_contract_matrix_gate_uses_code_size_block_decision(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    for rel in _required_matrix_paths():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# placeholder\n", encoding="utf-8")
    (tmp_path / "CODE_SIZE_REPORT.md").write_text(
        "- blocked: True\n"
        "- strict_scope_high_risk_findings: 1\n"
        "- strict_scope_soft_findings: 2\n"
        "- test_advisory_findings: 99\n",
        encoding="utf-8",
    )

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is False
    assert report.code_size_blocked is True
    assert report.high_risk == 1
    assert report.soft == 2
    assert {item["code"] for item in report.findings} == {"CODE_SIZE_GATE_BLOCKED"}


def test_offline_contract_matrix_keeps_advisory_code_size_counts_non_blocking(
    tmp_path: Path,
) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    for rel in _required_matrix_paths():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# placeholder\n", encoding="utf-8")
    (tmp_path / "CODE_SIZE_REPORT.md").write_text(
        "- blocked: False\n"
        "- strict_scope_high_risk_findings: 9\n"
        "- strict_scope_soft_findings: 2\n",
        encoding="utf-8",
    )

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is True
    assert report.high_risk == 9
    assert report.soft == 2
    assert report.findings == ()


def _required_matrix_paths() -> tuple[str, ...]:
    from scripts.check_offline_contract_matrix import REQUIRED_AREAS

    return tuple(path for paths in REQUIRED_AREAS.values() for path in paths)
