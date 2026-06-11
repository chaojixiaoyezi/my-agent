"""XLSX 产物验收合同。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import ArtifactFinding

# ---- XLSX 读取（原 artifact_xlsx_reader.py 并入）----
_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def workbook_text(workbook: ZipFile, names: set[str]) -> str:
    return "\n".join(_xml_text(workbook.read(name).decode("utf-8", errors="replace")) for name in _text_part_names(names))


def worksheet_tables(workbook: ZipFile, names: set[str]) -> list[list[list[str]]]:
    shared_strings = _shared_strings(workbook, names)
    return [
        _worksheet_rows(workbook.read(name).decode("utf-8", errors="replace"), shared_strings)
        for name in sorted(names)
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    ]


def required_columns_with_blank_values(
    tables: list[list[list[str]]],
    required_columns: list[str],
) -> list[str]:
    blank_columns: set[str] = set()
    for rows in tables:
        blank_columns.update(_blank_columns_for_table(rows, required_columns))
    return [column for column in required_columns if column in blank_columns]


def _blank_columns_for_table(rows: list[list[str]], required_columns: list[str]) -> set[str]:
    if not rows:
        return set()
    header = [str(cell).strip() for cell in rows[0]]
    return set().union(
        *(_blank_required_columns_in_row(header, row, required_columns) for row in rows[1:] if _row_has_data(row)),
    )


def _row_has_data(row: list[str]) -> bool:
    return any(str(cell).strip() for cell in row)


def _blank_required_columns_in_row(header: list[str], row: list[str], required_columns: list[str]) -> set[str]:
    blanks: set[str] = set()
    for column in required_columns:
        if column in header and not _row_value(row, header.index(column)):
            blanks.add(column)
    return blanks


def _row_value(row: list[str], index: int) -> str:
    return str(row[index]).strip() if index < len(row) else ""


def _text_part_names(names: set[str]) -> list[str]:
    return [
        name
        for name in sorted(names)
        if name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/")
    ]


def _shared_strings(workbook: ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t")) for item in root.iter(f"{_SHEET_NS}si")]


def _worksheet_rows(xml: str, shared_strings: list[str]) -> list[list[str]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [_row_values(row, shared_strings) for row in root.iter(f"{_SHEET_NS}row") if _row_values(row, shared_strings)]


def _row_values(row: ET.Element, shared_strings: list[str]) -> list[str]:
    values: dict[int, str] = {}
    for default_index, cell in enumerate(row.findall(f"{_SHEET_NS}c")):
        index = _cell_column_index(cell.get("r"), default_index)
        values[index] = _cell_value(cell, shared_strings)
    return [values.get(index, "") for index in range(max(values) + 1)] if values else []


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_SHEET_NS}t"))
    value = cell.find(f"{_SHEET_NS}v")
    text = "" if value is None or value.text is None else value.text
    if cell.get("t") == "s":
        return _shared_string_value(shared_strings, text)
    return text


def _shared_string_value(shared_strings: list[str], index_text: str) -> str:
    try:
        index = int(index_text)
    except ValueError:
        return ""
    return shared_strings[index] if 0 <= index < len(shared_strings) else ""


def _cell_column_index(ref: str | None, default_index: int) -> int:
    match = re.match(r"([A-Z]+)", str(ref or ""))
    if not match:
        return default_index
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


def _xml_text(xml: str) -> str:
    values = re.findall(r"<(?:t|v)(?:\\s[^>]*)?>(.*?)</(?:t|v)>", xml, flags=re.DOTALL)
    return "\n".join(unescape(_strip_xml_tags(value)) for value in values)


def _strip_xml_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


__all__ = ["required_columns_with_blank_values", "workbook_text", "worksheet_tables"]


# ---- XLSX 验收逻辑 ----
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
