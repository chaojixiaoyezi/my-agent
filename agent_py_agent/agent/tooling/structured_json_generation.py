# LLM: Structured row generation expands compact machine specs into JSON checkpoints.
# 模块用途: 为 write_structured_json 提供通用 generated_rows 生成能力，避免模型手写大表 JSON。

from __future__ import annotations

import math
from typing import Any

MAX_GENERATED_ROWS = 50000


# LLM: generated_rows_checkpoint builds rows/sheets from typed generation rules only.
# 函数用途: 将 generated_rows.count/columns/fields/sheets 扩展为 rows 和可选 sheets。
def generated_rows_checkpoint(spec: object) -> dict[str, object]:
    if not isinstance(spec, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows 必须是对象")
    count = _bounded_count(spec.get("count"))
    fields = _fields(spec.get("fields"))
    columns = _columns(spec.get("columns"), fields)
    rows = [_generated_row(index, columns, fields) for index in range(count)]
    payload: dict[str, object] = {"rows": rows}
    if columns:
        payload["columns"] = columns
    if sheets := _generated_sheets(rows, columns, spec.get("sheets")):
        payload["sheets"] = sheets
    return payload


# LLM: has_generated_rows detects the explicit machine parameter, not prose intent.
# 函数用途: 帮主工具判断是否存在 generated_rows 结构化生成请求。
def has_generated_rows(params: dict[str, Any]) -> bool:
    return isinstance(params.get("generated_rows"), dict)


# LLM: _generated_row evaluates fields in declared column order so derived fields can reference earlier values.
# 函数用途: 生成单行数据，支持常量、循环、数字序列、格式模板和乘法派生。
def _generated_row(index: int, columns: list[str], fields: dict[str, object]) -> dict[str, object]:
    row: dict[str, object] = {}
    for column in columns:
        row[column] = _field_value(fields.get(column), row, index)
    return row


# LLM: _field_value keeps row facts grounded in rule objects rather than natural-language descriptions.
# 函数用途: 根据一个字段规则计算单元格值；未知规则按稳定错误处理。
def _field_value(rule: object, row: dict[str, object], index: int) -> object:
    if not isinstance(rule, dict):
        return rule if rule is not None else ""
    if "value" in rule:
        return rule.get("value")
    if isinstance(rule.get("cycle"), list):
        return _cycle_value(rule["cycle"], index, rule.get("offset"))
    if isinstance(rule.get("number"), dict):
        return _number_value(_number_rule(rule), index)
    if isinstance(rule.get("multiply"), dict):
        return _multiply_value(rule["multiply"], row)
    if "source" in rule and "factor" in rule:
        return _multiply_value(rule, row)
    if "format" in rule:
        return _format_value(str(rule.get("format") or ""), rule, index)
    raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows 字段规则不支持")


# LLM: _cycle_value selects deterministic row values from a structured list.
# 函数用途: 处理月份、地区、品类等枚举字段的循环生成。
def _cycle_value(values: list[object], index: int, offset: object) -> object:
    usable = [item for item in values if item is not None]
    if not usable:
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows.cycle 不能为空")
    return usable[(index + _int_value(offset, default=0)) % len(usable)]


# LLM: _number_value creates bounded numeric series without executing arbitrary expressions.
# 函数用途: 处理 start/step/modulo/decimals 数字序列规则。
def _number_value(rule: dict[str, object], index: int) -> object:
    start = _float_value(rule.get("start"), default=0.0)
    step = _float_value(rule.get("step"), default=1.0)
    modulo = _float_value(rule.get("modulo"), default=0.0)
    value = start + step * index
    if modulo > 0:
        value = value % modulo
    return _rounded_number(value, decimals=_int_value(rule.get("decimals"), default=0))


# LLM: _number_rule merges shorthand numeric aliases into the nested number rule.
# 函数用途: 兼容模型把 modulo/decimals 写在 number 同级的结构化参数。
def _number_rule(rule: dict[str, object]) -> dict[str, object]:
    number = rule.get("number")
    if not isinstance(number, dict):
        return {}
    merged = dict(number)
    for key in ("modulo", "decimals"):
        if key in rule and key not in merged:
            merged[key] = rule[key]
    return merged


# LLM: _multiply_value supports simple derived numeric fields such as profit from sales.
# 函数用途: 从已生成字段读取源值，乘以 factor 后按 decimals 输出。
def _multiply_value(rule: dict[str, object], row: dict[str, object]) -> object:
    source = str(rule.get("source") or rule.get("field") or "").strip()
    if not source:
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows.multiply 缺少 source")
    base = _float_value(row.get(source), default=0.0)
    factor = _float_value(rule.get("factor"), default=1.0)
    decimals = _int_value(rule.get("decimals"), default=2)
    return _rounded_number(base * factor, decimals=decimals)


# LLM: _format_value exposes only index/index0 placeholders to avoid arbitrary expression parsing.
# 函数用途: 处理 ID 等模板字段，模板只能引用 index 和 index0。
def _format_value(template: str, rule: dict[str, object], index: int) -> str:
    start = _int_value(rule.get("start"), default=1)
    try:
        return template.format(index=index + start, index0=index)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows.format 模板无效") from exc


# LLM: _generated_sheets partitions generated rows into same-shape workbook-ready sheets.
# 函数用途: 根据 sheets.count/prefix 生成多 sheet checkpoint，保持每个 sheet 都有 columns 和 rows。
def _generated_sheets(
    rows: list[dict[str, object]], columns: list[str], spec: object
) -> list[dict[str, object]]:
    if spec is None or spec is False:
        return []
    if spec is True:
        spec = {"count": 1}
    if not isinstance(spec, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows.sheets 必须是对象或 true")
    count = _int_value(spec.get("count"), default=1)
    if count <= 0:
        return []
    chunk_size = max(1, math.ceil(len(rows) / count))
    prefix = str(spec.get("prefix") or "Sheet").strip() or "Sheet"
    return [
        {"name": f"{prefix}{index + 1}", "columns": columns, "rows": rows[index * chunk_size : (index + 1) * chunk_size]}
        for index in range(count)
        if rows[index * chunk_size : (index + 1) * chunk_size]
    ]


# LLM: _bounded_count prevents accidental memory blowups from a malformed generation spec.
# 函数用途: 校验 generated_rows.count 的范围。
def _bounded_count(value: object) -> int:
    count = _int_value(value, default=0)
    if count <= 0 or count > MAX_GENERATED_ROWS:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: generated_rows.count 必须在 1 到 {MAX_GENERATED_ROWS} 之间")
    return count


# LLM: _fields validates the field rule map without interpreting prose values.
# 函数用途: 读取 fields 对象并把 key 规范为字符串列名。
def _fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise ValueError("TOOL_INVALID_ARGUMENTS: generated_rows.fields 不能为空")
    return {str(key): item for key, item in value.items() if str(key).strip()}


# LLM: _columns keeps generated row order stable and explicit.
# 函数用途: 优先使用 columns 参数；缺省时按 fields 声明顺序生成列。
def _columns(value: object, fields: dict[str, object]) -> list[str]:
    if isinstance(value, list):
        columns = [str(item).strip() for item in value if str(item).strip()]
        if columns:
            return columns
    return list(fields)


# LLM: _rounded_number returns ints when possible so generated facts stay compact.
# 函数用途: 按 decimals 四舍五入，并把整数浮点转为 int。
def _rounded_number(value: float, *, decimals: int) -> object:
    rounded = round(value, max(0, decimals))
    return int(rounded) if float(rounded).is_integer() else rounded


# LLM: _int_value normalizes numeric rule fields.
# 函数用途: 将 JSON 数字或数字字符串转换为 int，坏值使用默认值。
def _int_value(value: object, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# LLM: _float_value normalizes numeric rule fields.
# 函数用途: 将 JSON 数字或数字字符串转换为 float，坏值使用默认值。
def _float_value(value: object, *, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


__all__ = ["generated_rows_checkpoint", "has_generated_rows"]
