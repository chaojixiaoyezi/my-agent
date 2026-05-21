# LLM: XLSX reader helpers extract workbook text and table cells for contract validators.
# 模块用途: 只负责读取 OOXML 结构，不决定验收语义，让 xlsx contract 入口保持很薄。

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html import unescape
from zipfile import ZipFile

_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# LLM: workbook_text extracts visible text from worksheets and shared strings.
# 函数用途: 从 xlsx XML 中提取单元格文本，支持 inline strings 和 sharedStrings。
def workbook_text(workbook: ZipFile, names: set[str]) -> str:
    return "\n".join(_xml_text(workbook.read(name).decode("utf-8", errors="replace")) for name in _text_part_names(names))


# LLM: worksheet_tables extracts worksheet cells into row arrays for schema value checks.
# 函数用途: 读取 workbook XML 和 sharedStrings，生成轻量行表，不依赖外部 spreadsheet 库。
def worksheet_tables(workbook: ZipFile, names: set[str]) -> list[list[list[str]]]:
    shared_strings = _shared_strings(workbook, names)
    return [
        _worksheet_rows(workbook.read(name).decode("utf-8", errors="replace"), shared_strings)
        for name in sorted(names)
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    ]


# LLM: required_columns_with_blank_values inspects worksheet rows without using natural-language task text.
# 函数用途: 找出出现在表头中、但任一有数据行对应单元格为空的 required_columns。
def required_columns_with_blank_values(
    tables: list[list[list[str]]],
    required_columns: list[str],
) -> list[str]:
    blank_columns: set[str] = set()
    for rows in tables:
        blank_columns.update(_blank_columns_for_table(rows, required_columns))
    return [column for column in required_columns if column in blank_columns]


# LLM: _blank_columns_for_table checks one worksheet table against required value columns.
# 函数用途: 跳过空表和空行，只检查有数据的行。
def _blank_columns_for_table(rows: list[list[str]], required_columns: list[str]) -> set[str]:
    if not rows:
        return set()
    header = [str(cell).strip() for cell in rows[0]]
    return set().union(
        *(_blank_required_columns_in_row(header, row, required_columns) for row in rows[1:] if _row_has_data(row)),
    )


# LLM: _row_has_data identifies data rows before required-column value checks.
# 函数用途: 全空行不触发必填列 finding。
def _row_has_data(row: list[str]) -> bool:
    return any(str(cell).strip() for cell in row)


# LLM: _blank_required_columns_in_row checks a single machine table row against its header.
# 函数用途: 当某个 required column 在当前 sheet 表头里存在时，要求每个有数据行该列非空。
def _blank_required_columns_in_row(header: list[str], row: list[str], required_columns: list[str]) -> set[str]:
    blanks: set[str] = set()
    for column in required_columns:
        if column in header and not _row_value(row, header.index(column)):
            blanks.add(column)
    return blanks


# LLM: _row_value returns a stripped cell value or empty text for sparse rows.
# 函数用途: sparse OOXML 行缺格时按空值处理。
def _row_value(row: list[str], index: int) -> str:
    return str(row[index]).strip() if index < len(row) else ""


# LLM: _text_part_names returns workbook XML parts that can contain visible text.
# 函数用途: 稳定排序工作簿文本来源，便于 required_columns 检查。
def _text_part_names(names: set[str]) -> list[str]:
    return [
        name
        for name in sorted(names)
        if name == "xl/sharedStrings.xml" or name.startswith("xl/worksheets/")
    ]


# LLM: _shared_strings reads the workbook shared string table when present.
# 函数用途: 支持第三方 xlsx 的 t="s" 单元格，不只支持本项目生成的 inlineStr。
def _shared_strings(workbook: ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    return ["".join(node.text or "" for node in item.iter(f"{_SHEET_NS}t")) for item in root.iter(f"{_SHEET_NS}si")]


# LLM: _worksheet_rows parses one worksheet into sparse-safe row arrays.
# 函数用途: 用 A1 单元格坐标还原列位置，保证空单元格能被 required column 检查发现。
def _worksheet_rows(xml: str, shared_strings: list[str]) -> list[list[str]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [_row_values(row, shared_strings) for row in root.iter(f"{_SHEET_NS}row") if _row_values(row, shared_strings)]


# LLM: _row_values reads one worksheet row into a sparse-safe array.
# 函数用途: 保留缺失单元格位置，避免列校验错位。
def _row_values(row: ET.Element, shared_strings: list[str]) -> list[str]:
    values: dict[int, str] = {}
    for fallback_index, cell in enumerate(row.findall(f"{_SHEET_NS}c")):
        index = _cell_column_index(cell.get("r"), fallback_index)
        values[index] = _cell_value(cell, shared_strings)
    return [values.get(index, "") for index in range(max(values) + 1)] if values else []


# LLM: _cell_value reads inline, shared-string, and plain worksheet cell values.
# 函数用途: 统一读取 OOXML 单元格文本，供必填列非空校验。
def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_SHEET_NS}t"))
    value = cell.find(f"{_SHEET_NS}v")
    text = "" if value is None or value.text is None else value.text
    if cell.get("t") == "s":
        return _shared_string_value(shared_strings, text)
    return text


# LLM: _shared_string_value dereferences a shared-string index safely.
# 函数用途: 无效 index 返回空字符串，让上层合同给出统一 finding。
def _shared_string_value(shared_strings: list[str], index_text: str) -> str:
    try:
        index = int(index_text)
    except ValueError:
        return ""
    return shared_strings[index] if 0 <= index < len(shared_strings) else ""


# LLM: _cell_column_index converts an A1 cell reference into a zero-based column index.
# 函数用途: 支持 sparse worksheet XML；没有 r 坐标时退回当前 cell 顺序。
def _cell_column_index(ref: str | None, fallback_index: int) -> int:
    match = re.match(r"([A-Z]+)", str(ref or ""))
    if not match:
        return fallback_index
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return max(0, index - 1)


# LLM: _xml_text extracts text nodes from XML without depending on spreadsheet libraries.
# 函数用途: 读取 <t> 和 <v> 节点文本，用于轻量 schema 验收。
def _xml_text(xml: str) -> str:
    values = re.findall(r"<(?:t|v)(?:\\s[^>]*)?>(.*?)</(?:t|v)>", xml, flags=re.DOTALL)
    return "\n".join(unescape(_strip_xml_tags(value)) for value in values)


# LLM: _strip_xml_tags removes rich-text child tags from extracted text fragments.
# 函数用途: 兼容 sharedStrings 富文本节点，保留人可见文本。
def _strip_xml_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value)


__all__ = ["required_columns_with_blank_values", "workbook_text", "worksheet_tables"]
