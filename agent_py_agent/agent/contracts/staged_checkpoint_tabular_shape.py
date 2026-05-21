# LLM: Staged checkpoint tabular shape checks validate source JSON before builders consume it.
# 模块用途: 对 sheets/rows/columns 的机器结构做通用校验，不读取任务 prompt 或报告正文。

from __future__ import annotations


# LLM: contains_nonempty_list checks structured data shape recursively without depending on prose content.
# 函数用途: 判断 JSON 里是否存在非空 list，适配 sheets/rows/items/projects/top10 等机器字段结构。
def contains_nonempty_list(value: object) -> bool:
    if isinstance(value, list):
        return bool(value) and any(list_item_has_data(item) for item in value)
    if isinstance(value, dict):
        return any(contains_nonempty_list(item) for item in value.values())
    return False


# LLM: tabular_json_shape_issue validates generic sheets/rows checkpoint structure before builder tools consume it.
# 函数用途: 对机器声明的表格 JSON 结构做通用检查：sheet 身份唯一、rows 非空、columns 与 row 字段一致。
def tabular_json_shape_issue(
    value: object,
    *,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> dict[str, str]:
    required = required_columns or []
    if not _needs_table_validation(value, required, required_sheets_min):
        return {}
    sheets = _tabular_sheet_candidates(value, required_columns=required)
    if not sheets:
        return {"code": "STAGED_JSON_NO_ROWS"}
    if count_issue := _sheet_count_issue(sheets, required_sheets_min):
        return count_issue
    return _first_sheet_shape_issue(sheets, required)


# LLM: _needs_table_validation keeps optional shape checks contract-driven.
# 函数用途: 只有合同要求列/sheet 或 JSON 自带 sheets 时才按表格结构验收。
def _needs_table_validation(value: object, required_columns: list[str], required_sheets_min: int) -> bool:
    return bool(required_columns or required_sheets_min or (isinstance(value, dict) and "sheets" in value))


# LLM: _sheet_count_issue validates the minimum declared sheet count.
# 函数用途: 缺少合同要求的 sheet 数时返回稳定 finding code。
def _sheet_count_issue(sheets: list[dict[str, object]], required_sheets_min: int) -> dict[str, str]:
    if required_sheets_min <= 0 or len(sheets) >= required_sheets_min:
        return {}
    return {
        "code": "STAGED_JSON_TOO_FEW_SHEETS",
        "sheet_count": str(len(sheets)),
        "required_sheets_min": str(required_sheets_min),
    }


# LLM: _first_sheet_shape_issue returns the first deterministic table-shape failure.
# 函数用途: 顺序检查 sheet 名称、rows、columns 和必填列空值。
def _first_sheet_shape_issue(sheets: list[dict[str, object]], required_columns: list[str]) -> dict[str, str]:
    seen_names: set[str] = set()
    for index, sheet in enumerate(sheets):
        if name_issue := _sheet_name_issue(sheet, index, seen_names):
            return name_issue
        rows = sheet.get("rows")
        if not isinstance(rows, list) or not any(list_item_has_data(row) for row in rows):
            return {"code": "STAGED_JSON_NO_ROWS", "sheet_name": str(sheet.get("name") or "")}
        if columns_issue := _sheet_columns_issue(sheet, rows, index, required_columns=required_columns):
            return columns_issue
    return {}


# LLM: _sheet_name_issue validates sheet identity fields.
# 函数用途: sheet.name 必须存在且唯一，方便后续 merge 和 artifact 定位。
def _sheet_name_issue(sheet: dict[str, object], index: int, seen_names: set[str]) -> dict[str, str]:
    name = str(sheet.get("name") or "").strip()
    if not name:
        return _shape_issue("sheet.name", index)
    if name in seen_names:
        return {"code": "STAGED_JSON_DUPLICATE_SHEET_NAMES", "sheet_name": name}
    seen_names.add(name)
    return {}


# LLM: _tabular_sheet_candidates normalizes flexible JSON table shapes into sheet-like records for shape validation.
# 函数用途: 把 rows/sheets/键值映射等表格 JSON 结构统一成 sheet 候选列表，供后续列和行校验复用。
def _tabular_sheet_candidates(value: object, *, required_columns: list[str]) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [{"name": "Sheet1", "rows": value}] if required_columns else []
    if not isinstance(value, dict):
        return []
    return _dict_sheet_candidates(value, required_columns)


# LLM: _dict_sheet_candidates handles dict-shaped source checkpoints.
# 函数用途: 支持显式 sheets、单表 rows，以及键值映射式多表。
def _dict_sheet_candidates(value: dict[str, object], required_columns: list[str]) -> list[dict[str, object]]:
    sheets = value.get("sheets")
    if isinstance(sheets, list):
        return [item for item in sheets if isinstance(item, dict)]
    if not required_columns:
        return []
    rows = value.get("rows")
    if isinstance(rows, list):
        return [{"name": str(value.get("name") or "Sheet1"), "columns": value.get("columns"), "rows": rows}]
    return [{"name": str(key), "rows": item} for key, item in value.items() if isinstance(item, list) and item]


# LLM: _sheet_columns_issue keeps workbook source rows aligned with their declared machine columns.
# 函数用途: 如果 sheet 声明了 columns，则校验 columns 是非空列表且每行包含这些字段，避免 builder 生成错表。
def _sheet_columns_issue(
    sheet: dict[str, object],
    rows: list[object],
    index: int,
    *,
    required_columns: list[str],
) -> dict[str, str]:
    columns = sheet.get("columns")
    if columns is None and not required_columns:
        return {}
    normalized = _normalized_columns(columns, rows, index)
    if isinstance(normalized, dict):
        return normalized
    if required_issue := _required_columns_issue(normalized, required_columns, index):
        return required_issue
    return _first_row_columns_issue(rows, normalized, required_columns, index)


# LLM: _normalized_columns returns declared or inferred table columns.
# 函数用途: columns 字段坏掉时返回 shape finding，否则返回列名数组。
def _normalized_columns(columns: object, rows: list[object], index: int) -> list[str] | dict[str, str]:
    if columns is not None and (not isinstance(columns, list) or not columns):
        return _shape_issue("sheet.columns", index)
    values = [str(column).strip() for column in columns] if isinstance(columns, list) else _row_columns(rows)
    return _shape_issue("sheet.columns", index) if any(not column for column in values) else values


# LLM: _required_columns_issue checks contract-required fields against table columns.
# 函数用途: 表格列缺必填字段时返回稳定 finding。
def _required_columns_issue(columns: list[str], required_columns: list[str], index: int) -> dict[str, str]:
    missing_required = [column for column in required_columns if column not in columns]
    if not missing_required:
        return {}
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "sheet_index": str(index),
        "missing_columns": ",".join(missing_required),
    }


# LLM: _first_row_columns_issue validates each data row against the effective column contract.
# 函数用途: 找到第一处行缺列或必填列空值问题。
def _first_row_columns_issue(
    rows: list[object],
    columns: list[str],
    required_columns: list[str],
    sheet_index: int,
) -> dict[str, str]:
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            return _shape_issue("sheet.rows", sheet_index, row_index=row_index)
        if missing := [column for column in columns if column not in row]:
            return _row_issue("STAGED_JSON_TABLE_SHAPE_INVALID", sheet_index, row_index, missing_columns=missing)
        if empty := [column for column in required_columns if column in columns and not _cell_value_present(row.get(column))]:
            return _row_issue("STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES", sheet_index, row_index, empty_columns=empty)
    return {}


# LLM: _row_issue formats row-level table findings.
# 函数用途: 统一 missing_columns 和 empty_columns 的机器字段。
def _row_issue(code: str, sheet_index: int, row_index: int, **fields: list[str]) -> dict[str, str]:
    issue = {"code": code, "sheet_index": str(sheet_index), "row_index": str(row_index)}
    issue.update({key: ",".join(value) for key, value in fields.items()})
    return issue


# LLM: _cell_value_present treats required table values as machine data, not header-only evidence.
# 函数用途: 判断表格必填字段是否有实际值；0/False 是有效值，空字符串和 None 不是。
def _cell_value_present(value: object) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


# LLM: _row_columns derives table fields from structured rows when a sheet omits explicit columns.
# 函数用途: 对未声明 columns 的表格 JSON，按 row 字段补出机器列名供 required_columns 校验。
def _row_columns(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            columns.extend(str(key) for key in row if str(key) not in columns)
    return columns


# LLM: _shape_issue returns stable table-shape status without embedding task-specific acceptance prose.
# 函数用途: 统一表格 JSON 结构错误的机器字段，供 closeout 和提交前校验复用。
def _shape_issue(field: str, index: int, *, row_index: int | None = None) -> dict[str, str]:
    issue = {
        "code": "STAGED_JSON_TABLE_SHAPE_INVALID",
        "field": field,
        "sheet_index": str(index),
    }
    if row_index is not None:
        issue["row_index"] = str(row_index)
    return issue


# LLM: list_item_has_data treats list entries as meaningful only when they carry real row/body payload.
# 函数用途: 区分“只有阶段骨架的列表项”和“已经有真实数据的列表项”。
def list_item_has_data(item: object) -> bool:
    if isinstance(item, list):
        return bool(item) and any(list_item_has_data(child) for child in item)
    if isinstance(item, dict):
        nested_values = [value for value in item.values() if isinstance(value, (list, dict))]
        if nested_values:
            return any(contains_nonempty_list(value) for value in nested_values)
        return any(_scalar_has_data(value) for value in item.values())
    return _scalar_has_data(item)


# LLM: _scalar_has_data keeps row detection structural while ignoring empty placeholders.
# 函数用途: 判断一个标量值是否算真实内容；空字符串和 None 不算，数字/布尔/非空字符串算。
def _scalar_has_data(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


__all__ = ["contains_nonempty_list", "tabular_json_shape_issue"]
