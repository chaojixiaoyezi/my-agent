from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook


def write_xlsx_fixture(
    workspace: Path,
    path: str | dict[str, Any],
    *,
    sheets: list[dict[str, Any]] | None = None,
    source_json_path: str | None = None,
) -> Path:
    path_text, sheets = _xlsx_fixture_inputs(workspace, path, sheets, source_json_path)
    target = workspace / path_text
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in sheets or [{"name": "Sheet1", "rows": []}]:
        _write_sheet(workbook, sheet)
    workbook.save(target)
    return target


def _write_sheet(workbook: Workbook, sheet: object) -> None:
    sheet_data = _sheet_data(sheet)
    worksheet = workbook.create_sheet(sheet_data["name"])
    rows = sheet_data["rows"]
    columns = sheet_data["columns"]
    if columns:
        worksheet.append([str(column) for column in columns])
    _append_rows(worksheet, rows, columns)


def _append_rows(worksheet: Any, rows: list[object], columns: list[str]) -> None:
    for row in rows:
        if isinstance(row, dict):
            worksheet.append([row.get(column, "") for column in columns])


def _xlsx_fixture_inputs(
    workspace: Path,
    path: str | dict[str, Any],
    sheets: list[dict[str, Any]] | None,
    source_json_path: str | None,
) -> tuple[str, list[dict[str, Any]] | None]:
    path_text, sheets, source_json_path = _direct_fixture_inputs(path, sheets, source_json_path)
    if not source_json_path:
        return path_text, sheets
    data = json.loads((workspace / source_json_path).read_text(encoding="utf-8"))
    raw_sheets = data.get("sheets") if isinstance(data, dict) else None
    return path_text, raw_sheets if isinstance(raw_sheets, list) else []


def _direct_fixture_inputs(
    path: str | dict[str, Any],
    sheets: list[dict[str, Any]] | None,
    source_json_path: str | None,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    if not isinstance(path, dict):
        return str(path), sheets, source_json_path
    raw_source = path.get("source_json_path")
    raw_sheets = path.get("sheets")
    return (
        str(path.get("path") or "report.xlsx"),
        raw_sheets if isinstance(raw_sheets, list) else sheets,
        str(raw_source) if raw_source else source_json_path,
    )


def _sheet_data(sheet: object) -> dict[str, Any]:
    if not isinstance(sheet, dict):
        return {"name": "Sheet1", "rows": [], "columns": []}
    rows = sheet.get("rows")
    columns = sheet.get("columns")
    normalized_rows = rows if isinstance(rows, list) else []
    normalized_columns = columns if isinstance(columns, list) else _columns_from_rows(normalized_rows)
    return {
        "name": str(sheet.get("name") or "Sheet1")[:31],
        "rows": normalized_rows,
        "columns": normalized_columns,
    }


def _columns_from_rows(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        _append_missing_columns(columns, row)
    return columns


def _append_missing_columns(columns: list[str], row: dict[str, Any]) -> None:
    known = set(columns)
    for key in row:
        text = str(key)
        if text not in known:
            columns.append(text)
            known.add(text)
