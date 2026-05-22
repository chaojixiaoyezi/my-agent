# LLM: Delivery recovery code mapping keeps validator-specific names behind one taxonomy bridge.
# 模块用途: 将 HTML/XLSX/PDF/JSON 等 finding code 归并为通用恢复错误类型。

from __future__ import annotations


def recovery_error_code(code: str) -> str:
    upper = str(code or "").upper()
    if upper in {"ARTIFACT_MISSING", "ARTIFACT_EMPTY"}:
        return "ARTIFACT_MISSING"
    if upper.startswith(("STAGED_", "PATH_", "SPREADSHEET_SOURCE_", "EVIDENCE_")):
        return upper
    if upper.startswith(("XLSX_", "CSV_", "JSON_", "PDF_", "HTML_")):
        return "ACCEPTANCE_FAILED"
    return "ACCEPTANCE_FAILED"


__all__ = ["recovery_error_code"]
