
from __future__ import annotations


def contains_nonempty_list(value: object) -> bool:
    if isinstance(value, list):
        return bool(value) and any(list_item_has_data(item) for item in value)
    if isinstance(value, dict):
        return any(contains_nonempty_list(item) for item in value.values())
    return False


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


def _needs_table_validation(value: object, required_columns: list[str], required_sheets_min: int) -> bool:
    return bool(required_columns or required_sheets_min or (isinstance(value, dict) and "sheets" in value))


def _sheet_count_issue(sheets: list[dict[str, object]], required_sheets_min: int) -> dict[str, str]:
    if required_sheets_min <= 0 or len(sheets) >= required_sheets_min:
        return {}
    return {
        "code": "STAGED_JSON_TOO_FEW_SHEETS",
        "sheet_count": str(len(sheets)),
        "required_sheets_min": str(required_sheets_min),
    }


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


def _sheet_name_issue(sheet: dict[str, object], index: int, seen_names: set[str]) -> dict[str, str]:
    name = str(sheet.get("name") or "").strip()
    if not name:
        return _shape_issue("sheet.name", index)
    if name in seen_names:
        return {"code": "STAGED_JSON_DUPLICATE_SHEET_NAMES", "sheet_name": name}
    seen_names.add(name)
    return {}


def _tabular_sheet_candidates(value: object, *, required_columns: list[str]) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [{"name": "Sheet1", "rows": value}] if required_columns else []
    if not isinstance(value, dict):
        return []
    return _dict_sheet_candidates(value, required_columns)


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


def _normalized_columns(columns: object, rows: list[object], index: int) -> list[str] | dict[str, str]:
    if columns is not None and (not isinstance(columns, list) or not columns):
        return _shape_issue("sheet.columns", index)
    values = [str(column).strip() for column in columns] if isinstance(columns, list) else _row_columns(rows)
    return _shape_issue("sheet.columns", index) if any(not column for column in values) else values


def _required_columns_issue(columns: list[str], required_columns: list[str], index: int) -> dict[str, str]:
    missing_required = [column for column in required_columns if column not in columns]
    if not missing_required:
        return {}
    return {
        "code": "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
        "sheet_index": str(index),
        "missing_columns": ",".join(missing_required),
    }


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
            return _row_issue("STAGED_JSON_TABLE_SHAPE_INVALID", sheet_index, row_index, {"missing_columns": missing})
        if empty := [column for column in required_columns if column in columns and not _cell_value_present(row.get(column))]:
            return _row_issue("STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES", sheet_index, row_index, {"empty_columns": empty})
    return {}


def _row_issue(code: str, sheet_index: int, row_index: int, fields: dict[str, list[str]]) -> dict[str, str]:
    issue = {"code": code, "sheet_index": str(sheet_index), "row_index": str(row_index)}
    issue.update({key: ",".join(value) for key, value in fields.items()})
    return issue


def _cell_value_present(value: object) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _row_columns(rows: list[object]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            columns.extend(str(key) for key in row if str(key) not in columns)
    return columns


def _shape_issue(field: str, index: int, *, row_index: int | None = None) -> dict[str, str]:
    issue = {
        "code": "STAGED_JSON_TABLE_SHAPE_INVALID",
        "field": field,
        "sheet_index": str(index),
    }
    if row_index is not None:
        issue["row_index"] = str(row_index)
    return issue


def list_item_has_data(item: object) -> bool:
    if isinstance(item, list):
        return bool(item) and any(list_item_has_data(child) for child in item)
    if isinstance(item, dict):
        nested_values = [value for value in item.values() if isinstance(value, (list, dict))]
        if nested_values:
            return any(contains_nonempty_list(value) for value in nested_values)
        return any(_scalar_has_data(value) for value in item.values())
    return _scalar_has_data(item)


def _scalar_has_data(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


__all__ = ["contains_nonempty_list", "tabular_json_shape_issue"]
