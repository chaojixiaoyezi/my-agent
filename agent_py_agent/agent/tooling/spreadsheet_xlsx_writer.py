# LLM: Spreadsheet xlsx writer serializes normalized sheets into OOXML packages.
# 模块用途: 用标准库生成最小有效 xlsx，保持 data_to_workbook 工具入口轻量。

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from .spreadsheet_builder_models import WorkbookSheet


# LLM: write_xlsx_workbook serializes normalized sheets into a minimal valid OOXML workbook.
# 函数用途: 用标准库写 xlsx zip 包，不新增依赖，输出可被通用验收器检查。
def write_xlsx_workbook(path: Path, sheets: tuple[WorkbookSheet, ...]) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheet))


# LLM: _content_types_xml lists workbook parts for the xlsx package.
# 函数用途: 生成 OOXML 内容类型清单，sheet 数量来自结构化 sheets。
def _content_types_xml(sheet_count: int) -> str:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{sheet_overrides}</Types>"
    )


# LLM: _root_rels_xml connects the package root to the workbook.
# 函数用途: 生成 xlsx 根关系文件。
def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


# LLM: _workbook_xml creates workbook sheet entries with unique relationship ids.
# 函数用途: 生成 workbook.xml，工作表名字只来自结构化 sheet 名。
def _workbook_xml(sheets: tuple[WorkbookSheet, ...]) -> str:
    entries = "".join(
        f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{entries}</sheets></workbook>"
    )


# LLM: _workbook_rels_xml maps sheet ids and styles into package parts.
# 函数用途: 生成 workbook 关系文件，保证每个 sheet 有可解析 target。
def _workbook_rels_xml(sheet_count: int) -> str:
    sheet_rels = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    style_id = sheet_count + 1
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{style_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/></Relationships>'
    )


# LLM: _styles_xml supplies the minimal style part expected by spreadsheet readers.
# 函数用途: 生成最小 styles.xml，避免 workbook 被部分查看器误判异常。
def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '</styleSheet>'
    )


# LLM: _sheet_xml writes headers and rows as inline strings.
# 函数用途: 生成单个 worksheet XML，所有单元格来自结构化 columns/rows。
def _sheet_xml(sheet: WorkbookSheet) -> str:
    rows = [_row_xml(1, sheet.columns, {column: column for column in sheet.columns})]
    rows.extend(
        _row_xml(index, sheet.columns, row)
        for index, row in enumerate(sheet.rows, start=2)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )


# LLM: _row_xml turns one structured row into OOXML cells.
# 函数用途: 按列顺序生成一行，缺失字段输出空单元格。
def _row_xml(row_index: int, columns: tuple[str, ...], row: dict[str, object]) -> str:
    cells = "".join(
        _cell_xml(_cell_ref(column_index, row_index), _cell_text(row.get(column, "")))
        for column_index, column in enumerate(columns, start=1)
    )
    return f'<row r="{row_index}">{cells}</row>'


# LLM: _cell_xml serializes a value as an inline string cell.
# 函数用途: 转义单元格文本，避免特殊字符破坏 xlsx XML。
def _cell_xml(cell_ref: str, text: str) -> str:
    return f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'


# LLM: _cell_text converts JSON scalars and nested values into readable cell text.
# 函数用途: 对复杂值写 JSON 字符串，普通值写文本，保持内容可读。
def _cell_text(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


# LLM: _cell_ref converts a 1-based column and row index to Excel A1 notation.
# 函数用途: 生成单元格坐标，例如 A1、B2、AA3。
def _cell_ref(column_index: int, row_index: int) -> str:
    letters = ""
    value = column_index
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_index}"


__all__ = ["write_xlsx_workbook"]
