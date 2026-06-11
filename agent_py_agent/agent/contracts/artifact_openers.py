
from __future__ import annotations

"""产物格式打开器注册表（第 1 层）。

每个打开器只做一件事：把文件 bytes 变成结构化视图，或在打不开时返回一个
结构化的"打不开"finding。打开器**不做内容合同校验**——那是第 2 层通用检查器的事。
未登记的格式没有打开器，验收链路据此落回第 0 层（存在/非空/残桩，格式无关），
绝不"不在表里就拒绝"。

先构造结构化表示，再独立校验；打开器与内容验证正交。
新增一种简单格式 ≈ 在 OPENERS 注册一行；带格式专属深度校验的 ≈ 再加一个薄检查函数。
"""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .artifact_acceptance_models import ArtifactFinding
from .artifact_xlsx_contract import workbook_text, worksheet_tables


@dataclass(frozen=True)
class TabularView:
    """表格类格式（csv/xlsx）的结构化视图。"""

    rows: list[list[str]] = field(default_factory=list)        # csv 解析后的行
    names: set[str] = field(default_factory=set)               # xlsx zip 成员名
    text: str = ""                                             # xlsx 全文本（列名匹配用）
    tables: list[list[list[str]]] = field(default_factory=list)  # xlsx 每个 sheet 的行


@dataclass(frozen=True)
class JsonView:
    """JSON 解析后的顶层值。"""

    value: object


@dataclass(frozen=True)
class OpenedArtifact:
    """打开结果：opened=True 带 view；否则带 finding（打不开）。

    view 为 None 且 finding 为 None 表示该格式没有结构化视图（如 pdf/docx/txt/md
    的质量校验直接读文件），打开成功但不产视图。
    """

    kind: str
    view: object | None = None
    finding: ArtifactFinding | None = None

    @property
    def opened(self) -> bool:
        return self.finding is None


def open_csv(path: Path) -> OpenedArtifact:
    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    except csv.Error as exc:
        return OpenedArtifact("csv", finding=ArtifactFinding(code="CSV_INVALID", severity="hard", message=f"Invalid CSV: {exc}"))
    return OpenedArtifact("csv", view=TabularView(rows=rows))


def open_xlsx(path: Path) -> OpenedArtifact:
    try:
        with ZipFile(path) as workbook:
            names = set(workbook.namelist())
            text = workbook_text(workbook, names)
            tables = worksheet_tables(workbook, names)
    except (BadZipFile, OSError) as exc:
        return OpenedArtifact("xlsx", finding=ArtifactFinding(code="XLSX_INVALID", severity="hard", message=f"Invalid XLSX package: {exc}"))
    missing_parts = _missing_xlsx_parts(names)
    if missing_parts:
        return OpenedArtifact("xlsx", finding=ArtifactFinding(
            code="XLSX_INVALID_PACKAGE",
            severity="hard",
            message="XLSX package is missing required workbook parts.",
            value=",".join(missing_parts),
        ))
    open_finding = _xlsx_workbook_open_finding(path)
    if open_finding is not None:
        return OpenedArtifact("xlsx", finding=open_finding)
    return OpenedArtifact("xlsx", view=TabularView(names=names, text=text, tables=tables))


def open_pdf(path: Path) -> OpenedArtifact:
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        return OpenedArtifact("pdf", finding=ArtifactFinding(
            code="PDF_INVALID_SIGNATURE",
            severity="hard",
            message="PDF is missing %PDF header or EOF marker.",
        ))
    return OpenedArtifact("pdf")


def open_docx(path: Path) -> OpenedArtifact:
    try:
        with ZipFile(path) as docx:
            names = set(docx.namelist())
    except (BadZipFile, OSError) as exc:
        return OpenedArtifact("docx", finding=ArtifactFinding("DOCX_INVALID", "hard", f"Invalid DOCX package: {exc}"))
    if "word/document.xml" not in names:
        return OpenedArtifact("docx", finding=ArtifactFinding("DOCX_MISSING_DOCUMENT_XML", "hard", "DOCX lacks word/document.xml."))
    return OpenedArtifact("docx")


def open_json(path: Path) -> OpenedArtifact:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return OpenedArtifact("json", finding=ArtifactFinding(
            code="JSON_INVALID",
            severity="hard",
            message=f"Invalid JSON: {exc.msg}",
            location=str(exc.pos),
        ))
    if not isinstance(value, (dict, list)):
        return OpenedArtifact("json", finding=ArtifactFinding(
            code="JSON_UNEXPECTED_TOP_LEVEL",
            severity="hard",
            message="JSON top-level must be object or array.",
        ))
    return OpenedArtifact("json", view=JsonView(value=value))


def _missing_xlsx_parts(names: set[str]) -> list[str]:
    missing: list[str] = []
    if "[Content_Types].xml" not in names:
        missing.append("[Content_Types].xml")
    if "xl/workbook.xml" not in names:
        missing.append("xl/workbook.xml")
    if not any(name.startswith("xl/worksheets/") and name.endswith(".xml") for name in names):
        missing.append("xl/worksheets/*.xml")
    return missing


def _xlsx_workbook_open_finding(path: Path) -> ArtifactFinding | None:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None
    try:
        return _xlsx_sheet_presence_finding(load_workbook(path, read_only=True, data_only=True))
    except Exception as exc:
        return ArtifactFinding(
            code="XLSX_INVALID_PACKAGE",
            severity="hard",
            message=f"XLSX cannot be opened by the workbook reader: {exc}",
        )


def _xlsx_sheet_presence_finding(workbook) -> ArtifactFinding | None:
    try:
        if not workbook.sheetnames:
            return ArtifactFinding("XLSX_NO_VISIBLE_SHEETS", "hard", "XLSX workbook has no visible sheets.")
        return None
    finally:
        workbook.close()


# 格式 → 打开器的薄注册表。未登记格式没有打开器 → 验收落第 0 层。
OPENERS = {
    "csv": open_csv,
    "xlsx": open_xlsx,
    "pdf": open_pdf,
    "docx": open_docx,
    "json": open_json,
}


def opener_for(kind: str):
    """返回登记的打开器，未登记返回 None（不抛错，交给第 0 层）。"""
    return OPENERS.get(kind)


__all__ = [
    "JsonView",
    "OpenedArtifact",
    "TabularView",
    "OPENERS",
    "open_csv",
    "open_docx",
    "open_json",
    "open_pdf",
    "open_xlsx",
    "opener_for",
]
