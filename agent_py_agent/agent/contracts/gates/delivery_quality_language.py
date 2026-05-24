# LLM: Delivery language quality checks validate declared localized fields.
# 模块用途: 根据 language_contract.fields 检查字段目标语言，不从 prompt 猜交付要求。

from __future__ import annotations

from typing import Any

from ..evidence_contract import EvidenceClaim
from ..staged_checkpoint_evidence_payloads import string_list
from .models import GateFinding


# LLM: delivery_quality_language_findings checks declared target-language fields.
# 函数用途: 只根据 language_contract.fields 检查对应值，避免从用户 prompt 猜测哪些字段该是中文。
def delivery_quality_language_findings(
    payload: dict[str, Any],
    language_contract: object,
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    if not isinstance(language_contract, dict):
        return []
    target = _text(language_contract.get("target_language"))
    fields = string_list(language_contract.get("fields"))
    if target != "zh" or not fields:
        return []
    min_cjk = _int_value(language_contract.get("min_cjk_chars"), default=4)
    max_latin_ratio = _float_value(language_contract.get("max_latin_ratio"), default=0.45)
    return [
        _language_finding(field, value, location, target)
        for field, value, location in _language_candidates(payload, claim_records, fields)
        if not _looks_like_zh(value, min_cjk=min_cjk, max_latin_ratio=max_latin_ratio)
    ]


# LLM: _language_candidates extracts declared fields from rows first, then claims.
# 函数用途: 优先检查交付行里的字段值；没有行数据时再检查 claim.value。
def _language_candidates(
    payload: dict[str, Any],
    claim_records: list[EvidenceClaim],
    fields: list[str],
) -> list[tuple[str, str, str]]:
    rows = _row_records(payload)
    if rows:
        return [
            (field, str(row.get(field) or ""), f"items[{index}].{field}")
            for index, row in enumerate(rows)
            for field in fields
            if field in row
        ]
    return [
        (claim.field, str(claim.value or ""), f"claims.{claim.claim_id}")
        for claim in claim_records
        if claim.field in fields
    ]


# LLM: _row_records returns table-like row dictionaries from common structured payload slots.
# 函数用途: 读取 items/rows/data.rows/sheets.rows 等机器字段，不扫描正文。
def _row_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if rows := _list_of_dicts(payload.get("items")):
        return rows
    if rows := _list_of_dicts(payload.get("rows")):
        return rows
    data = payload.get("data")
    if isinstance(data, dict) and (rows := _list_of_dicts(data.get("rows"))):
        return rows
    return _sheet_rows(payload.get("sheets"))


# LLM: _sheet_rows extracts rows from structured sheet payloads.
# 函数用途: 支持任意结构化 JSON 的 sheets[].rows 形状，保持 _row_records 低嵌套。
def _sheet_rows(value: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for sheet in value:
        if isinstance(sheet, dict):
            rows.extend(_list_of_dicts(sheet.get("rows")))
    return rows


# LLM: _list_of_dicts normalizes table-like arrays.
# 函数用途: 将显式 list[dict] 规整成拷贝后的 row 字典列表。
def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


# LLM: _language_finding creates stable language mismatch diagnostics.
# 函数用途: 记录字段、位置、目标语言和字符统计，供修复流程定位。
def _language_finding(field: str, value: str, location: str, target: str) -> GateFinding:
    return GateFinding(
        "LANGUAGE_FIELD_TARGET_MISMATCH",
        evidence={
            "field": field,
            "location": location,
            "target_language": target,
            "cjk_chars": _cjk_count(value),
            "latin_ratio": _latin_ratio(value),
        },
    )


# LLM: _looks_like_zh applies a small deterministic language signal for declared zh fields.
# 函数用途: 用字符统计做目标语言门，不进行 LLM 质量评分。
def _looks_like_zh(value: str, *, min_cjk: int, max_latin_ratio: float) -> bool:
    text = value.strip()
    if not text:
        return False
    return _cjk_count(text) >= min_cjk and _latin_ratio(text) <= max_latin_ratio


# LLM: _cjk_count counts Chinese/Japanese/Korean unified ideographs.
# 函数用途: 为 zh 目标语言字段提供稳定字符统计。
def _cjk_count(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


# LLM: _latin_ratio estimates whether text is mostly Latin letters.
# 函数用途: 辅助判断中文字段是否被英文占位内容填充。
def _latin_ratio(value: str) -> float:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return 0.0
    latin = [char for char in letters if "a" <= char.lower() <= "z"]
    return len(latin) / len(letters)


# LLM: _float_value parses optional numeric policy values.
# 函数用途: 合同阈值字段不是数字时按默认值处理，不从文本里猜。
def _float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# LLM: _int_value parses optional integer policy values.
# 函数用途: 合同数字字段不是整数时按默认值处理。
def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# LLM: _text normalizes scalar values for exact comparisons only.
# 函数用途: 将结构化标量转成去空白字符串，不解释自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["delivery_quality_language_findings"]
