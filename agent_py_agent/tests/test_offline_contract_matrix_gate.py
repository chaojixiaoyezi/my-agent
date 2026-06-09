from __future__ import annotations

from pathlib import Path


def test_offline_contract_matrix_gate_passes_current_repo() -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix, main

    repo_root = Path(__file__).resolve().parents[2]

    report = check_offline_contract_matrix(repo_root)

    assert report.ok is True
    assert report.missing_areas == ()
    assert report.high_risk == 0
    assert report.soft == 0
    assert main(["--repo-root", str(repo_root), "--json"]) == 0


def test_offline_contract_matrix_gate_reports_missing_files(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    (tmp_path / "CODE_SIZE_REPORT.md").write_text(
        "- strict_scope_high_risk_findings: 0\n- strict_scope_soft_findings: 0\n- test_advisory_findings: 9\n",
        encoding="utf-8",
    )

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is False
    assert "concurrency" in report.missing_areas
    assert report.findings[0]["code"] == "OFFLINE_MATRIX_FILE_MISSING"


def test_offline_contract_matrix_gate_reports_code_size_findings(tmp_path: Path) -> None:
    from scripts.check_offline_contract_matrix import check_offline_contract_matrix

    for rel in _required_matrix_paths():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# placeholder\n", encoding="utf-8")
    (tmp_path / "CODE_SIZE_REPORT.md").write_text(
        "- strict_scope_high_risk_findings: 1\n"
        "- strict_scope_soft_findings: 2\n"
        "- test_advisory_findings: 99\n",
        encoding="utf-8",
    )

    report = check_offline_contract_matrix(tmp_path)

    assert report.ok is False
    assert report.high_risk == 1
    assert report.soft == 2
    assert {"CODE_SIZE_HIGH_RISK_NOT_ZERO", "CODE_SIZE_SOFT_NOT_ZERO"} <= {item["code"] for item in report.findings}


def _required_matrix_paths() -> tuple[str, ...]:
    from scripts.check_offline_contract_matrix import REQUIRED_AREAS

    return tuple(path for paths in REQUIRED_AREAS.values() for path in paths)
