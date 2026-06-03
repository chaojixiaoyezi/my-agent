
from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import ArtifactFinding
from .artifact_xlsx_reader import (
    required_columns_with_blank_values,
    workbook_text,
    worksheet_tables,
)


def xlsx_contract_findings(path: Path, validation_contract: dict[str, object] | None) -> list[ArtifactFinding]:
    contract = validation_contract or {}
    findings: list[ArtifactFinding] = []
    try:
        with ZipFile(path) as workbook:
            names = set(workbook.namelist())
            text = workbook_text(workbook, names)
            tables = worksheet_tables(workbook, names)
    except (BadZipFile, OSError):
        return findings
    findings.extend(_sheet_count_findings(path, names, contract))
    findings.extend(_required_column_findings(path, text, contract))
    findings.extend(_required_column_value_findings(path, tables, contract))
    return findings


def _sheet_count_findings(
    path: Path,
    names: set[str],
    contract: dict[str, object],
) -> list[ArtifactFinding]:
    required = _positive_int(contract.get("required_sheets_min"))
    if required <= 0:
        return []
    actual = len([name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")])
    if actual >= required:
        return []
    return [
        ArtifactFinding(
            code="XLSX_TOO_FEW_SHEETS",
            severity="hard",
            message=f"Workbook has {actual} sheets, expected at least {required}.",
            location=str(path),
            value=str(actual),
        )
    ]


def _required_column_findings(
    path: Path,
    workbook_text: str,
    contract: dict[str, object],
) -> list[ArtifactFinding]:
    required_columns = _required_columns(contract.get("required_columns"))
    if not required_columns:
        return []
    missing = [column for column in required_columns if column not in workbook_text]
    if not missing:
        return []
    return [
        ArtifactFinding(
            code="XLSX_MISSING_REQUIRED_COLUMNS",
            severity="hard",
            message="Workbook is missing required columns.",
            location=str(path),
            value=",".join(missing),
        )
    ]


def _required_column_value_findings(
    path: Path,
    worksheet_tables: list[list[list[str]]],
    contract: dict[str, object],
) -> list[ArtifactFinding]:
    required_columns = _required_columns(contract.get("required_columns"))
    if not required_columns:
        return []
    blank_columns = required_columns_with_blank_values(worksheet_tables, required_columns)
    if not blank_columns:
        return []
    return [
        ArtifactFinding(
            code="XLSX_REQUIRED_COLUMN_EMPTY_VALUES",
            severity="hard",
            message="Workbook has blank values in required columns.",
            location=str(path),
            value=",".join(blank_columns),
        )
    ]


def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


__all__ = ["xlsx_contract_findings"]
