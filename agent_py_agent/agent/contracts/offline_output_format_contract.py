# LLM: Offline output format contracts validate deliverable shape from structured facts.
# 模块用途: 校验 Markdown、JSON、XLSX、编码和大输出外置事实，避免模型用“已完成”绕过格式验收。

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings


# LLM: OfflineOutputFormatValidation reports output-format contract findings.
# 类用途: 返回输出格式离线合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineOutputFormatValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


# LLM: validate_output_format_contract checks output records without reading task prose.
# 函数用途: 根据 outputs 里的 kind、字段、sheet、编码和外置引用等机器字段校验产物格式。
def validate_output_format_contract(contract: dict[str, Any]) -> OfflineOutputFormatValidation:
    findings: list[dict[str, object]] = []
    for output in _record_list(contract.get("outputs")):
        _validate_markdown_output(output, findings)
        _validate_json_output(output, findings)
        _validate_xlsx_output(output, findings)
        _validate_encoding(output, findings)
        _validate_long_output(output, findings)
    return OfflineOutputFormatValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_output_format", findings),
    )


# LLM: _validate_markdown_output checks headings and table count for Markdown outputs.
# 函数用途: 根据 required_sections/required_tables_min 与 headings/table_count 结构事实做验收。
def _validate_markdown_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) not in {"md", "markdown"}:
        return
    output_id = _output_id(output)
    headings = set(_headings(output))
    missing_sections = [section for section in _string_list(output.get("required_sections")) if section not in headings]
    if missing_sections:
        findings.append(_finding("MARKDOWN_SECTION_MISSING", output_id, {"missing_sections": missing_sections}))
    required_tables = _positive_int(output.get("required_tables_min"))
    if required_tables and _positive_int(output.get("table_count")) < required_tables:
        findings.append(
            _finding(
                "MARKDOWN_TABLE_MISSING",
                output_id,
                {"table_count": _positive_int(output.get("table_count")), "required_tables_min": required_tables},
            )
        )


# LLM: _validate_json_output checks top-level JSON field and evidence-count facts.
# 函数用途: 根据 required_fields 和 evidence_count 结构字段校验 JSON 报告。
def _validate_json_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) != "json":
        return
    fields = set(_string_list(output.get("fields")))
    missing_fields = [field for field in _string_list(output.get("required_fields")) if field not in fields]
    if missing_fields:
        findings.append(_finding("JSON_SCHEMA_FIELD_MISSING", _output_id(output), {"missing_fields": missing_fields}))
    required_evidence = _positive_int(output.get("required_evidence_min"))
    if required_evidence and _positive_int(output.get("evidence_count")) < required_evidence:
        findings.append(
            _finding(
                "JSON_EVIDENCE_MISSING",
                _output_id(output),
                {"evidence_count": _positive_int(output.get("evidence_count"))},
            )
        )


# LLM: _validate_xlsx_output checks workbook sheet, column, and row facts.
# 函数用途: 根据 required_sheets、required_columns 和 required_rows_min 校验表格产物结构。
def _validate_xlsx_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) != "xlsx":
        return
    sheet_names = set(_string_list(output.get("sheet_names")))
    missing_sheets = [sheet for sheet in _string_list(output.get("required_sheets")) if sheet not in sheet_names]
    if missing_sheets:
        findings.append(_finding("XLSX_SHEET_MISSING", _output_id(output), {"missing_sheets": missing_sheets}))
    columns = set(_string_list(output.get("columns")))
    missing_columns = [column for column in _string_list(output.get("required_columns")) if column not in columns]
    if missing_columns:
        findings.append(_finding("XLSX_COLUMN_MISSING", _output_id(output), {"missing_columns": missing_columns}))
    required_rows = _positive_int(output.get("required_rows_min"))
    if required_rows and _positive_int(output.get("row_count")) < required_rows:
        findings.append(_finding("XLSX_ROW_MISSING", _output_id(output), {"row_count": _positive_int(output.get("row_count"))}))


# LLM: _validate_encoding trusts explicit decoder facts instead of guessing from text content.
# 函数用途: 如果输出记录声明 encoding=UTF-8 且 encoding_valid=false，则返回编码错误。
def _validate_encoding(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _text(output.get("encoding")).lower().replace("_", "-") != "utf-8":
        return
    if output.get("encoding_valid") is False:
        findings.append(_finding("UTF8_ENCODING_INVALID", _output_id(output)))


# LLM: _validate_long_output requires large inline bodies to be externalized.
# 函数用途: inline_bytes 超过预算时必须有 artifact_refs 或 truncated=true。
def _validate_long_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    budget = _positive_int(output.get("inline_budget_bytes"))
    if budget <= 0 or _positive_int(output.get("inline_bytes")) <= budget:
        return
    if output.get("truncated") is True or _string_list(output.get("artifact_refs")):
        return
    findings.append(
        _finding(
            "LONG_OUTPUT_NOT_EXTERNALIZED",
            _output_id(output),
            {"inline_bytes": _positive_int(output.get("inline_bytes")), "inline_budget_bytes": budget},
        )
    )


# LLM: _headings reads declared heading facts or Markdown heading syntax as format facts.
# 函数用途: 优先使用 headings 数组；缺失时从 text 的 Markdown 标题行提取，不理解正文语义。
def _headings(output: dict[str, Any]) -> list[str]:
    headings = _string_list(output.get("headings"))
    if headings:
        return headings
    text = _text(output.get("text"))
    return [
        match.group(1).strip()
        for line in text.splitlines()
        for match in [re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)]
        if match
    ]


# LLM: _finding creates compact output-format findings.
# 函数用途: 生成 code、output_id 和可选结构化字段。
def _finding(code: str, output_id: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, "output_id": output_id, **(extra or {})}


# LLM: _record_list normalizes output records.
# 函数用途: 只接受 dict 列表，忽略无结构项。
def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: _kind returns normalized artifact kind identifiers.
# 函数用途: 读取 kind 字段并转小写字符串。
def _kind(output: dict[str, Any]) -> str:
    return _text(output.get("kind")).lower()


# LLM: _output_id returns stable finding location ids.
# 函数用途: 读取 output_id/id/path，供 finding 定位。
def _output_id(output: dict[str, Any]) -> str:
    return _text(output.get("output_id") or output.get("id") or output.get("path"))


# LLM: _string_list normalizes list-like contract fields.
# 函数用途: 把 required_fields/columns/sheets 等字段转成非空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _positive_int parses optional numeric limits.
# 函数用途: 将行数、大小预算、表格数量等字段规整为非负整数。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineOutputFormatValidation", "validate_output_format_contract"]
