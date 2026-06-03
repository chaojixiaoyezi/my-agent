
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
