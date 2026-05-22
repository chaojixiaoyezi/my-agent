# LLM: CSV artifact acceptance validates table shape outside the artifact dispatcher.
# 模块用途: 独立校验 CSV header、数据行阈值和列合同，让 artifact_acceptance 保持路由职责。

from __future__ import annotations

import csv
from pathlib import Path

from .artifact_acceptance_models import ArtifactAcceptanceReport, ArtifactFinding
from .artifact_structured_contracts import csv_contract_findings


# LLM: validate_csv_artifact checks CSV structure and declared column contracts.
# 函数用途: 验证 CSV header、数据行阈值和 required_columns，返回统一 ArtifactAcceptanceReport。
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


# LLM: _csv_min_data_rows reads the structured minimum row threshold.
# 函数用途: 将 validation_contract.min_data_rows 转成非负整数，非法值回退默认 1。
def _csv_min_data_rows(validation_contract: dict[str, object]) -> int:
    try:
        return max(0, int(validation_contract.get("min_data_rows", 1)))
    except (TypeError, ValueError):
        return 1


# LLM: _report_with_finding builds a one-finding CSV report.
# 函数用途: 避免 CSV 早失败分支重复构造 ArtifactAcceptanceReport。
def _report_with_finding(path: Path, finding: ArtifactFinding) -> ArtifactAcceptanceReport:
    return ArtifactAcceptanceReport(ok=False, artifact_ref=str(path), artifact_kind="csv", findings=[finding])


__all__ = ["validate_csv_artifact"]
