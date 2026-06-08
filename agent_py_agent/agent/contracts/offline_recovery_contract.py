
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from .recovery import RecoveryAction
from .recovery import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload

CORRUPT_STATE_ERROR_CODES = {"JSON_DECODE_ERROR", "STATE_SCHEMA_INVALID", "STATE_CHECKSUM_MISMATCH"}
NON_RETRYABLE_ERROR_CODES = {"PATH_PERMISSION_DENIED", "WRITE_FORBIDDEN", "APPROVAL_REJECTED"}


@dataclass(frozen=True)
class OfflineRecoveryValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    actions: tuple[dict[str, str], ...]
    recovery: dict[str, Any] | None = None


def validate_recovery_events(events: tuple[dict[str, Any], ...]) -> OfflineRecoveryValidation:
    findings: list[dict[str, str]] = []
    actions: list[dict[str, str]] = []
    for event in events:
        _apply_recovery_event(event, findings, actions)
    return OfflineRecoveryValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        actions=tuple(actions),
        recovery=_offline_recovery_payload(findings, actions),
    )


def _offline_recovery_payload(
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not findings:
        return None
    next_statuses = {str(item.get("next_status") or "") for item in actions}
    status = "RECOVERING" if "VERIFYING" in next_statuses or "RECOVERING" in next_statuses else "BLOCKED"
    envelope = recovery_envelope_from_gate_payload(
        RecoveryEnvelopeRequest(
            gate="offline_recovery",
            status=status,
            allowed=False,
            findings=[
                {"code": item.get("code", ""), "severity": "P1", "message": "", "evidence": dict(item)}
                for item in findings
            ],
            recommended_action=str(actions[0].get("code") if actions else RecoveryAction.REPORT_BLOCKER.value),
            evidence={"actions": [dict(item) for item in actions]},
        )
    )
    return envelope.to_dict() if envelope is not None else None


def _apply_recovery_event(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    event_type = _event_type(event)
    if event_type == "tool_result":
        _handle_tool_result(event, findings, actions)
    if event_type == "state_load_result":
        _handle_state_load_result(event, findings, actions)
    if event_type == "finalizer_crash":
        _handle_finalizer_crash(event, findings, actions)


def _handle_tool_result(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    if event.get("ok") is not False:
        return
    if not _retryable(event):
        findings.append(_finding("NON_RETRYABLE_FAILURE", event, "tool failure is not retryable"))
        actions.append(_action("STOP_RETRY", event, next_status="BLOCKED"))
        return
    retry_limit = _retry_limit(event)
    if retry_limit > 0 and _attempt(event) >= retry_limit:
        findings.append(_finding("RETRY_LIMIT_EXCEEDED", event, "retry limit reached"))
        actions.append(_action("STOP_RETRY", event, next_status="BLOCKED"))
        return
    actions.append(_action("RETRY_ALLOWED", event, next_status="RUNNING"))


def _handle_state_load_result(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    if event.get("ok") is not False:
        return
    if _error_code(event) not in CORRUPT_STATE_ERROR_CODES:
        return
    findings.append(_finding("STATE_CORRUPT", event, _text(event.get("state_ref"))))
    actions.append(_action("STATE_CORRUPT", event, next_status="BLOCKED"))


def _handle_finalizer_crash(
    event: dict[str, Any],
    findings: list[dict[str, str]],
    actions: list[dict[str, str]],
) -> None:
    artifact_refs = sequence_strings(event.get("artifact_refs"))
    if not artifact_refs:
        findings.append(_finding("FINALIZER_CRASH_WITHOUT_ARTIFACT", event, "artifact_refs missing"))
        actions.append(_action("RERUN_WITH_IDEMPOTENCY_CHECK", event, next_status="BLOCKED"))
        return
    findings.append(_finding("REVALIDATE_ARTIFACT_BEFORE_RERUN", event, artifact_refs[0]))
    actions.append(
        _action(
            "REVALIDATE_ARTIFACT_BEFORE_RERUN",
            event,
            next_status="VERIFYING",
            extra={
                "forbid_reexecute_operation_ids": ",".join(
                    sequence_strings(event.get("executed_side_effect_operation_ids"))
                )
            },
        )
    )


def _retryable(event: dict[str, Any]) -> bool:
    if "retryable" in event:
        return bool(event.get("retryable"))
    return _error_code(event) not in NON_RETRYABLE_ERROR_CODES


def _attempt(event: dict[str, Any]) -> int:
    return _int_field(event.get("attempt"))


def _retry_limit(event: dict[str, Any]) -> int:
    if "retry_limit" not in event:
        return 1
    return max(0, _int_field(event.get("retry_limit")))


def _action(
    code: str,
    event: dict[str, Any],
    *,
    next_status: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    return {
        "code": code,
        "operation_id": _text(event.get("operation_id")),
        "run_id": _text(event.get("run_id")),
        "next_status": next_status,
        **(extra or {}),
    }


def _finding(code: str, event: dict[str, Any], detail: str) -> dict[str, str]:
    return {
        "code": code,
        "operation_id": _text(event.get("operation_id")),
        "run_id": _text(event.get("run_id")),
        "detail": detail,
    }


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


def _error_code(event: dict[str, Any]) -> str:
    return _text(event.get("error_code") or event.get("code")).upper()


def _int_field(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

__all__ = ["OfflineRecoveryValidation", "validate_recovery_events"]
