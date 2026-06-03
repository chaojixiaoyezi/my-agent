
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

WAITING_STATUSES = {"WAITING_FOR_TOOL", "WAITING_FOR_USER", "WAITING_FOR_CHILD", "WAITING_HUMAN"}
SUMMARY_REQUIRED_FIELDS = (
    "current_status",
    "completed_actions",
    "pending_actions",
    "evidence_refs",
    "next_constraints",
)


@dataclass(frozen=True)
class OfflineCompactResumeValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_compact_resume_bundle(bundle: dict[str, Any]) -> OfflineCompactResumeValidation:
    findings: list[dict[str, object]] = []
    pre_compact = _section(bundle.get("pre_compact"))
    compact = _section(bundle.get("compact"))
    _validate_waiting_state(pre_compact, compact, findings)
    _validate_tool_result_refs(pre_compact, compact, findings)
    _validate_failed_operations(bundle, compact, findings)
    _validate_side_effect_replay(compact, _event_list(bundle.get("resume_events")), findings)
    _validate_no_progress_replay(compact, _event_list(bundle.get("resume_events")), findings)
    _validate_summary(compact, findings)
    return OfflineCompactResumeValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_compact_resume", findings),
    )


def _validate_waiting_state(
    pre_compact: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    previous_status = _status(pre_compact.get("current_status"))
    if previous_status not in WAITING_STATUSES:
        return
    compact_status = _status(compact.get("current_status"))
    if compact_status != previous_status:
        findings.append(
            _finding(
                "COMPACT_STATE_MISSING",
                {
                    "expected_status": previous_status,
                    "compact_status": compact_status,
                    "waiting_reason": _text(pre_compact.get("waiting_reason")),
                },
            )
        )


def _validate_tool_result_refs(
    pre_compact: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    previous_refs = set(sequence_strings(pre_compact.get("tool_result_refs")))
    if not previous_refs:
        return
    compact_refs = set(sequence_strings(compact.get("tool_result_refs")))
    missing_refs = sorted(previous_refs - compact_refs)
    if missing_refs:
        findings.append(_finding("COMPACT_TOOL_REF_MISSING", {"missing_refs": missing_refs}))


def _validate_failed_operations(
    bundle: dict[str, Any],
    compact: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    failed_ids = set(sequence_strings(_section(bundle.get("pre_compact")).get("failed_operation_ids")))
    for event in _event_list(bundle.get("pre_compact_events")):
        if operation_id := _failed_event_operation_id(event):
            failed_ids.add(operation_id)
    if not failed_ids:
        return
    compact_failed_ids = set(sequence_strings(compact.get("failed_operation_ids")))
    missing_ids = sorted(failed_ids - compact_failed_ids)
    if missing_ids:
        findings.append(_finding("COMPACT_FAILURE_FORGOTTEN", {"missing_operation_ids": missing_ids}))


def _failed_event_operation_id(event: dict[str, Any]) -> str:
    if _event_type(event) != "tool_result" or event.get("ok") is not False:
        return ""
    return _text(event.get("operation_id"))


def _validate_side_effect_replay(
    compact: dict[str, Any],
    resume_events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    executed_ids = set(sequence_strings(compact.get("executed_side_effect_operation_ids")))
    if not executed_ids:
        return
    replayed = sorted(
        _text(event.get("operation_id"))
        for event in resume_events
        if _event_type(event) == "tool_call" and _text(event.get("operation_id")) in executed_ids
    )
    if replayed:
        findings.append(_finding("DANGEROUS_OPERATION_REPLAYED", {"operation_ids": replayed}))


def _validate_no_progress_replay(
    compact: dict[str, Any],
    resume_events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    stalled = set(sequence_strings(compact.get("no_progress_fingerprints")))
    if not stalled:
        return
    repeated = sorted(
        _text(event.get("progress_fingerprint"))
        for event in resume_events
        if _event_type(event) == "tool_call" and _text(event.get("progress_fingerprint")) in stalled
    )
    if repeated:
        findings.append(_finding("COMPACT_NO_PROGRESS_LOOP_REPEATED", {"progress_fingerprints": repeated}))


def _validate_summary(compact: dict[str, Any], findings: list[dict[str, object]]) -> None:
    summary = _section(compact.get("summary"))
    missing_fields = [
        field
        for field in SUMMARY_REQUIRED_FIELDS
        if _field_missing(summary, field)
    ]
    if missing_fields:
        findings.append(_finding("COMPACT_SUMMARY_INCOMPLETE", {"missing_fields": missing_fields}))


def _field_missing(summary: dict[str, Any], field: str) -> bool:
    value = summary.get(field)
    if isinstance(value, (list, tuple, set)):
        return not sequence_strings(value)
    return not _text(value)


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


def _event_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


def _status(value: object) -> str:
    return _text(value).upper()


__all__ = ["OfflineCompactResumeValidation", "validate_compact_resume_bundle"]
