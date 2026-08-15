
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

TERMINAL_APPROVAL_STATUSES = {"APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}


@dataclass(frozen=True)
class OfflineConcurrencyValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    recovery: dict[str, object] | None = None


def validate_concurrency_events(events: tuple[dict[str, Any], ...]) -> OfflineConcurrencyValidation:
    findings: list[dict[str, str]] = []
    _validate_leases(events, findings)
    _validate_artifact_writes(events, findings)
    _validate_approval_decisions(events, findings)
    return OfflineConcurrencyValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_concurrency", findings),
    )


def _validate_leases(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    active_by_run: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "lease_claimed" or _inactive(event):
            continue
        run_id = _text(event.get("run_id"))
        worker_id = _text(event.get("worker_id"))
        previous = active_by_run.get(run_id)
        if previous is not None and _text(previous.get("worker_id")) != worker_id:
            findings.append(_lease_conflict(run_id, previous, event))
            continue
        active_by_run[run_id] = event


def _validate_artifact_writes(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    writes_by_ref: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "artifact_write":
            continue
        ref = _text(event.get("artifact_ref") or event.get("path"))
        previous = writes_by_ref.get(ref)
        if previous is not None and not _same_artifact_operation(previous, event):
            findings.append(_artifact_conflict(ref, previous, event))
            continue
        writes_by_ref[ref] = event


def _validate_approval_decisions(events: tuple[dict[str, Any], ...], findings: list[dict[str, str]]) -> None:
    final_by_approval: dict[str, dict[str, Any]] = {}
    for event in events:
        if _event_type(event) != "approval_decision":
            continue
        status = _status(event.get("status"))
        if status not in TERMINAL_APPROVAL_STATUSES:
            continue
        approval_id = _text(event.get("approval_id"))
        previous = final_by_approval.get(approval_id)
        if previous is not None and _status(previous.get("status")) != status:
            findings.append(_approval_conflict(approval_id, previous, event))
            continue
        final_by_approval[approval_id] = event


def _same_artifact_operation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    key = _text(left.get("idempotency_key"))
    return bool(key) and key == _text(right.get("idempotency_key"))


def _lease_conflict(run_id: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "LEASE_ALREADY_HELD",
        "run_id": run_id,
        "held_by": _text(previous.get("worker_id")),
        "requested_by": _text(current.get("worker_id")),
    }


def _artifact_conflict(ref: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "ARTIFACT_WRITE_CONFLICT",
        "artifact_ref": ref,
        "first_run_id": _text(previous.get("run_id")),
        "second_run_id": _text(current.get("run_id")),
    }


def _approval_conflict(approval_id: str, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    return {
        "code": "APPROVAL_DECISION_ALREADY_FINAL",
        "approval_id": approval_id,
        "first_status": _status(previous.get("status")),
        "second_status": _status(current.get("status")),
    }


def _inactive(event: dict[str, Any]) -> bool:
    if event.get("active") is False:
        return True
    return _status(event.get("status")) in {"RELEASED", "EXPIRED", "CANCELLED"}


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


def _status(value: object) -> str:
    return _text(value).upper()

__all__ = ["OfflineConcurrencyValidation", "validate_concurrency_events"]
