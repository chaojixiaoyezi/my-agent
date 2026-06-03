
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class OfflineOutputFormatValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


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


def _validate_markdown_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) not in {"md", "markdown"}:
        return
    output_id = _output_id(output)
    headings = set(_headings(output))
    missing_sections = [section for section in sequence_strings(output.get("required_sections")) if section not in headings]
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


def _validate_json_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) != "json":
        return
    fields = set(sequence_strings(output.get("fields")))
    missing_fields = [field for field in sequence_strings(output.get("required_fields")) if field not in fields]
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


def _validate_xlsx_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _kind(output) != "xlsx":
        return
    sheet_names = set(sequence_strings(output.get("sheet_names")))
    missing_sheets = [sheet for sheet in sequence_strings(output.get("required_sheets")) if sheet not in sheet_names]
    if missing_sheets:
        findings.append(_finding("XLSX_SHEET_MISSING", _output_id(output), {"missing_sheets": missing_sheets}))
    columns = set(sequence_strings(output.get("columns")))
    missing_columns = [column for column in sequence_strings(output.get("required_columns")) if column not in columns]
    if missing_columns:
        findings.append(_finding("XLSX_COLUMN_MISSING", _output_id(output), {"missing_columns": missing_columns}))
    required_rows = _positive_int(output.get("required_rows_min"))
    if required_rows and _positive_int(output.get("row_count")) < required_rows:
        findings.append(_finding("XLSX_ROW_MISSING", _output_id(output), {"row_count": _positive_int(output.get("row_count"))}))


def _validate_encoding(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _text(output.get("encoding")).lower().replace("_", "-") != "utf-8":
        return
    if output.get("encoding_valid") is False:
        findings.append(_finding("UTF8_ENCODING_INVALID", _output_id(output)))


def _validate_long_output(output: dict[str, Any], findings: list[dict[str, object]]) -> None:
    budget = _positive_int(output.get("inline_budget_bytes"))
    if budget <= 0 or _positive_int(output.get("inline_bytes")) <= budget:
        return
    if output.get("truncated") is True or sequence_strings(output.get("artifact_refs")):
        return
    findings.append(
        _finding(
            "LONG_OUTPUT_NOT_EXTERNALIZED",
            _output_id(output),
            {"inline_bytes": _positive_int(output.get("inline_bytes")), "inline_budget_bytes": budget},
        )
    )


def _headings(output: dict[str, Any]) -> list[str]:
    headings = sequence_strings(output.get("headings"))
    if headings:
        return headings
    text = _text(output.get("text"))
    return [
        match.group(1).strip()
        for line in text.splitlines()
        for match in [re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)]
        if match
    ]


def _finding(code: str, output_id: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, "output_id": output_id, **(extra or {})}


def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _kind(output: dict[str, Any]) -> str:
    return _text(output.get("kind")).lower()


def _output_id(output: dict[str, Any]) -> str:
    return _text(output.get("output_id") or output.get("id") or output.get("path"))


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)

__all__ = ["OfflineOutputFormatValidation", "validate_output_format_contract"]
