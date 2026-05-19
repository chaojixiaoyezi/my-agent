# LLM: Spreadsheet builder tools convert structured data refs into workbook artifacts.
# 模块用途: 提供通用 data_to_workbook 工具，让模型交结构化数据，系统稳定生成 xlsx。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._filesystem_helpers import _required_path, _text_param
from ._filesystem_read import FileSystemTool
from .models import ToolExecutionResult, ToolSpec
from .spreadsheet_builder_models import WorkbookSheet
from .spreadsheet_xlsx_writer import write_xlsx_workbook


# LLM: DataWorkbookTool builds xlsx artifacts from JSON or inline structured sheet data.
# 类用途: 给主代理和后续子代理提供通用表格产物工具，减少现场生成大脚本导致的超时。
class DataWorkbookTool(FileSystemTool):

    # LLM: DataWorkbookTool.__init__ declares the stable tool schema and retrieval metadata.
    # 函数用途: 初始化 data_to_workbook 工具规格，说明 source_json_path、sheets 和输出路径。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.spec = ToolSpec(
            name="data_to_workbook",
            category="artifact",
            description="把结构化 JSON 数据或 sheets 参数生成 xlsx 工作簿。",
            use_cases=[
                "已经有 rows/source_data.json，需要稳定生成 Excel/xlsx",
                "资料整理、榜单、统计结果要交付 workbook，避免临时写大脚本",
            ],
            avoid_when=[
                "还没有结构化行数据时，先收集数据并写 source_data.json",
            ],
            keywords=[
                "xlsx",
                "excel",
                "workbook",
                "spreadsheet",
                "表格",
                "数据整理",
                "生成工作簿",
                "source_data",
            ],
            parameters={
                "path": "要写出的 xlsx 路径",
                "source_json_path": "可选，包含 rows/sheets 或顶层列表数据的 JSON 文件路径",
                "sheets": "可选，直接传入工作表列表，每个包含 name、columns、rows",
                "default_sheet_name": "可选，从顶层 rows 生成单表时的默认表名",
            },
            parameter_details={
                "path": "相对工作区的 .xlsx 输出路径；父目录会自动创建。",
                "source_json_path": "相对工作区的 JSON 路径；JSON 必须包含非空 list 数据。",
                "sheets": "数组；每个 sheet 可包含 name、columns、rows。rows 支持对象数组或数组行。",
                "default_sheet_name": "没有 sheet 名时使用，默认 Sheet1。",
            },
            examples=[
                '{"tool": "data_to_workbook", "source_json_path": "outputs/report/source_data.json", "path": "outputs/report/report.xlsx"}',
                '{"tool": "data_to_workbook", "path": "out.xlsx", "sheets": [{"name": "Summary", "rows": [{"项目": "demo"}]}]}',
            ],
        )

    # LLM: DataWorkbookTool.execute validates params, extracts tables, and writes one workbook artifact.
    # 函数用途: 执行结构化数据到 xlsx 的转换；失败时返回稳定错误码而不是让模型猜原因。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _workbook_request_from_params(params)
            target = self.resolve_path(request.path)
            source_value = _source_value_from_request(request, self)
            sheets = _sheets_from_value(source_value, default_sheet_name=request.default_sheet_name)
        except ValueError as exc:
            return ToolExecutionResult("data_to_workbook", False, str(exc), error_code=_error_code(exc))
        target.parent.mkdir(parents=True, exist_ok=True)
        write_xlsx_workbook(target, sheets)
        payload = {
            "artifact_ref": self.display_path(target),
            "sheet_count": len(sheets),
            "row_count": sum(len(sheet.rows) for sheet in sheets),
            "sheets": [{"name": sheet.name, "rows": len(sheet.rows)} for sheet in sheets],
        }
        return ToolExecutionResult(
            "data_to_workbook",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={"output": payload},
        )


# LLM: _WorkbookBuildRequest keeps data_to_workbook params bundled.
# 类用途: 保存输出路径、可选源 JSON、可选 sheets 和默认 sheet 名。
@dataclass(frozen=True)
class _WorkbookBuildRequest:
    path: str
    source_json_path: str
    sheets: object
    default_sheet_name: str


# LLM: _workbook_request_from_params parses tool params without accepting prose facts.
# 函数用途: 从 JSON 工具参数中提取构建请求，要求 source_json_path 或 sheets 至少有一个。
def _workbook_request_from_params(params: dict[str, Any]) -> _WorkbookBuildRequest:
    output_path = _required_path(params.get("path"))
    source_path = ""
    if params.get("source_json_path") is not None:
        source_path = _required_path(params.get("source_json_path"), name="source_json_path")
    sheets = params.get("sheets")
    if not source_path and not isinstance(sheets, list):
        raise ValueError("SPREADSHEET_SOURCE_MISSING: 需要 source_json_path 或 sheets")
    return _WorkbookBuildRequest(
        path=output_path,
        source_json_path=source_path,
        sheets=sheets,
        default_sheet_name=_text_param(
            params.get("default_sheet_name", "Sheet1"),
            name="default_sheet_name",
            max_chars=80,
            strip=True,
        ),
    )


# LLM: _source_value_from_request loads machine JSON or falls back to inline sheets.
# 函数用途: 读取 source_json_path 指向的结构化数据；不解析 prompt 或日志正文。
def _source_value_from_request(request: _WorkbookBuildRequest, tool: FileSystemTool) -> object:
    if request.source_json_path:
        source_path = tool.resolve_path(request.source_json_path)
        if not source_path.exists():
            raise ValueError(f"SPREADSHEET_SOURCE_MISSING: source_json_path 不存在: {request.source_json_path}")
        try:
            return json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"SPREADSHEET_SOURCE_INVALID: JSON 解析失败: {exc.msg}") from exc
    return {"sheets": request.sheets}


# LLM: _sheets_from_value extracts non-empty table-shaped lists from structured data.
# 函数用途: 把 JSON 顶层列表、rows、sheets 或多个 list 字段转换成工作表集合。
def _sheets_from_value(value: object, *, default_sheet_name: str) -> tuple[WorkbookSheet, ...]:
    candidates = _sheet_candidates(value, default_sheet_name=default_sheet_name)
    sheets = tuple(sheet for sheet in (_normalize_sheet(item) for item in candidates) if sheet.rows)
    if not sheets:
        raise ValueError("SPREADSHEET_SOURCE_NO_ROWS: 结构化数据里没有非空表格行")
    return sheets


# LLM: _sheet_candidates recognizes common machine data shapes for table output.
# 函数用途: 从结构化对象中找 rows/sheets 或顶层非空列表字段，生成候选工作表。
def _sheet_candidates(value: object, *, default_sheet_name: str) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [{"name": default_sheet_name, "rows": value}]
    if not isinstance(value, dict):
        return []
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        return [dict(item) for item in sheets if isinstance(item, dict)]
    rows = value.get("rows")
    if isinstance(rows, list):
        return [{"name": str(value.get("name") or default_sheet_name), "columns": value.get("columns"), "rows": rows}]
    candidates: list[dict[str, object]] = []
    for key, item in value.items():
        if isinstance(item, list) and item:
            candidates.append({"name": str(key), "rows": item})
    return candidates


# LLM: _normalize_sheet converts one candidate into a typed sheet with stable columns.
# 函数用途: 统一对象行、数组行和标量行，保证写 xlsx 时不依赖自然语言字段。
def _normalize_sheet(candidate: dict[str, object]) -> WorkbookSheet:
    rows_value = candidate.get("rows")
    rows = rows_value if isinstance(rows_value, list) else []
    normalized_rows = tuple(_normalize_row(item) for item in rows)
    columns = _columns_for_sheet(candidate.get("columns"), normalized_rows)
    return WorkbookSheet(
        name=_clean_sheet_name(str(candidate.get("name") or "Sheet1")),
        columns=tuple(columns),
        rows=normalized_rows,
    )


# LLM: _normalize_row converts arbitrary JSON row values into cell dictionaries.
# 函数用途: 支持 dict、list 和标量三种结构化行，避免模型为简单表格写转换脚本。
def _normalize_row(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, list):
        return {f"col_{index}": item for index, item in enumerate(value, start=1)}
    return {"value": value}


# LLM: _columns_for_sheet preserves explicit columns and otherwise unions row keys.
# 函数用途: 稳定生成表头，保证中文字段和新增字段都能进入 workbook。
def _columns_for_sheet(raw_columns: object, rows: tuple[dict[str, object], ...]) -> list[str]:
    columns = _explicit_columns(raw_columns)
    if columns:
        return columns
    return _row_columns(rows) or ["value"]


# LLM: _explicit_columns normalizes caller-provided column order.
# 函数用途: 提取显式 columns 字段，避免 _columns_for_sheet 出现过深嵌套。
def _explicit_columns(raw_columns: object) -> list[str]:
    if not isinstance(raw_columns, list):
        return []
    return [str(item) for item in raw_columns if str(item).strip()]


# LLM: _row_columns unions keys from structured rows while preserving first-seen order.
# 函数用途: 根据行数据生成表头，保持新增字段可见且顺序稳定。
def _row_columns(rows: tuple[dict[str, object], ...]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        _append_missing_columns(columns, row)
    return columns


# LLM: _append_missing_columns keeps row column discovery shallow and reusable.
# 函数用途: 将一行里的新字段追加到 columns，保持首次出现顺序。
def _append_missing_columns(columns: list[str], row: dict[str, object]) -> None:
    columns.extend(key for key in row if key not in columns)


# LLM: _clean_sheet_name keeps generated sheet names valid and deterministic.
# 函数用途: 清理 Excel 不支持的 sheet 名字符并限制长度。
def _clean_sheet_name(name: str) -> str:
    cleaned = "".join("_" if char in "[]:*?/\\\\" else char for char in name).strip()
    return (cleaned or "Sheet1")[:31]


# LLM: _error_code extracts stable builder error codes from ValueError messages.
# 函数用途: 把工具失败转为机器可读 error_code，方便恢复流程判断下一步。
def _error_code(exc: ValueError) -> str:
    text = str(exc)
    prefix = text.split(":", 1)[0].strip()
    if prefix.startswith("SPREADSHEET_"):
        return prefix
    if "路径" in text or "path" in text.lower():
        return "PATH_INVALID"
    return "TOOL_INVALID_ARGUMENTS"


__all__ = ["DataWorkbookTool"]
