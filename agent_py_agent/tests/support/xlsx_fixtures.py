from __future__ import annotations

# LLM: XLSX test fixtures use real workbooks after the dedicated data_to_workbook tool was retired.
# 模块用途: 给合同测试生成最小真实 XLSX 文件，避免依赖已删除的模型可见专项工具。
import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook


# LLM: write_xlsx_fixture is a test helper, not a production model tool.
# 函数用途: 根据测试参数或 source JSON 生成真实 XLSX fixture。
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


# LLM: _write_sheet appends one fixture sheet to a workbook.
# 函数用途: 根据规整后的 sheet 数据写入表头和行内容。
def _write_sheet(workbook: Workbook, sheet: object) -> None:
    sheet_data = _sheet_data(sheet)
    worksheet = workbook.create_sheet(sheet_data["name"])
    rows = sheet_data["rows"]
    columns = sheet_data["columns"]
    if columns:
        worksheet.append([str(column) for column in columns])
    _append_rows(worksheet, rows, columns)


# LLM: _append_rows writes dict rows with the selected column order.
# 函数用途: 将测试行数据追加到工作表中，忽略非 dict 行。
def _append_rows(worksheet: Any, rows: list[object], columns: list[str]) -> None:
    for row in rows:
        if isinstance(row, dict):
            worksheet.append([row.get(column, "") for column in columns])


# LLM: _xlsx_fixture_inputs normalizes both old dict-style and direct fixture calls.
# 函数用途: 从测试传参或 source JSON 中解析目标路径和 sheets。
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


# LLM: _direct_fixture_inputs supports legacy dict params used by older tests.
# 函数用途: 标准化直接传入的 path/sheets/source_json_path。
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


# LLM: _sheet_data keeps workbook row/column defaults in one place.
# 函数用途: 把单个 sheet fixture 规整成 name、rows、columns。
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


# LLM: _columns_from_rows derives a stable header order from dict rows.
# 函数用途: 测试没有显式 columns 时按首次出现顺序推导列名。
def _columns_from_rows(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        _append_missing_columns(columns, row)
    return columns


# LLM: _append_missing_columns appends unseen keys while preserving order.
# 函数用途: 避免列推导函数里出现嵌套循环判断。
def _append_missing_columns(columns: list[str], row: dict[str, Any]) -> None:
    known = set(columns)
    for key in row:
        text = str(key)
        if text not in known:
            columns.append(text)
            known.add(text)
