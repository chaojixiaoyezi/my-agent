
from __future__ import annotations

import csv
from pathlib import Path

from .artifact_acceptance_models import ArtifactAcceptanceReport, ArtifactFinding
from .artifact_structured_contracts import csv_contract_findings


def validate_csv_artifact(
    path: Path,
    validation_contract: dict[str, object] | None = None,
) -> ArtifactAcceptanceReport:
    contract = validation_contract or {}
    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    except csv.Error as exc:
        finding = ArtifactFinding(code="CSV_INVALID", severity="hard", message=f"Invalid CSV: {exc}")
        return _report_with_finding(path, finding)
    if not rows or not any(cell.strip() for cell in rows[0]):
        finding = ArtifactFinding(
            code="CSV_HEADER_MISSING",
            severity="hard",
            message="CSV must include a non-empty header row.",
        )
        return _report_with_finding(path, finding)
    data_rows = [row for row in rows[1:] if any(cell.strip() for cell in row)]
    min_data_rows = _csv_min_data_rows(contract)
    if len(data_rows) < min_data_rows:
        finding = ArtifactFinding(
            code="CSV_INSUFFICIENT_DATA_ROWS",
            severity="hard",
            message="CSV data row count is below validation_contract.min_data_rows.",
            value=f"{len(data_rows)}<{min_data_rows}",
        )
        return _report_with_finding(path, finding)
    findings = csv_contract_findings(path, rows, contract)
    return ArtifactAcceptanceReport(
        ok=not any(item.severity == "hard" for item in findings),
        artifact_ref=str(path),
        artifact_kind="csv",
        findings=findings,
    )


def _csv_min_data_rows(validation_contract: dict[str, object]) -> int:
    try:
        return max(0, int(validation_contract.get("min_data_rows", 1)))
    except (TypeError, ValueError):
        return 1


def _report_with_finding(path: Path, finding: ArtifactFinding) -> ArtifactAcceptanceReport:
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind="csv", findings=[finding])


__all__ = ["validate_csv_artifact"]
