# LLM: XLSX contract checks validate workbook structure declared by artifact contracts.
# 模块用途: 检查 xlsx 的 sheet 数和必需列，避免空壳工作簿通过真实 E2E。

from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import ArtifactFinding
from .artifact_xlsx_reader import (
    required_columns_with_blank_values,
    workbook_text,
    worksheet_tables,
)


# LLM: xlsx_contract_findings returns schema findings derived only from validation_contract.
# 函数用途: 根据 required_sheets_min 和 required_columns 检查 workbook，不解析任务 prompt。
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


# LLM: _sheet_count_findings checks required sheet count against worksheet parts.
# 函数用途: 当合同要求多个 sheet 时，确保 xlsx 包里有足够 worksheet。
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


# LLM: _required_column_findings checks declared column labels against workbook XML text.
# 函数用途: 确认必需列名出现在工作簿文本单元格中，避免错表或空表通过。
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


# LLM: _required_column_value_findings checks required spreadsheet fields contain values in data rows.
# 函数用途: 对已声明 required_columns 的表格，拒绝表头存在但数据行空值的 workbook。
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


# LLM: _required_columns normalizes contract column labels.
# 函数用途: 从结构化 required_columns 字段读取列名列表。
def _required_columns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


# LLM: _positive_int parses optional numeric contract limits.
# 函数用途: 将 required_sheets_min 转为正整数，非法值按未设置处理。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


__all__ = ["xlsx_contract_findings"]
