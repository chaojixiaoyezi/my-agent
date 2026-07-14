from __future__ import annotations

from collections.abc import Iterable
from typing import Any

_MAX_ACCEPTANCE_SUMMARY_CHARS = 6000


def acceptance_user_summary(records: Iterable[object]) -> str:
    """Return the latest successful submit-for-acceptance user summary.

    The summary remains an untrusted model-authored field until the channel
    projection sanitizes it.  Keeping it structured here lets the delivery
    closeout preserve useful completion facts without exposing the surrounding
    machine protocol.
    """
    rows = [record for record in records if isinstance(record, dict)]
    for record in reversed(rows):
        if _record_tool(record) != "submit_for_acceptance" or not _record_succeeded(record):
            continue
        parameters = record.get("parameters")
        if not isinstance(parameters, dict):
            continue
        value = parameters.get("summary") or parameters.get("note")
        summary = _normalized_summary(value)
        if summary:
            return summary
    return ""


def attach_acceptance_user_summary(report: dict[str, Any], value: object) -> dict[str, Any]:
    summary = _normalized_summary(value)
    if summary:
        report["user_summary"] = summary
    return report


def _record_tool(record: dict[str, Any]) -> str:
    return str(record.get("tool") or record.get("tool_name") or "").strip()


def _record_succeeded(record: dict[str, Any]) -> bool:
    if record.get("ok") is False or record.get("success") is False:
        return False
    return str(record.get("status") or "ok").strip().lower() not in {
        "error",
        "failed",
        "failure",
        "cancelled",
        "canceled",
    }


def _normalized_summary(value: object) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text:
        return ""
    if len(text) > _MAX_ACCEPTANCE_SUMMARY_CHARS:
        return text[:_MAX_ACCEPTANCE_SUMMARY_CHARS].rstrip() + "…"
    return text


__all__ = ["acceptance_user_summary", "attach_acceptance_user_summary"]
