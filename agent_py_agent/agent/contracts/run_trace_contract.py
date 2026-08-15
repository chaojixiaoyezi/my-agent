
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings
from .state_machine_transitions import transition_contract


@dataclass(frozen=True)
class RunTraceValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, str], ...]
    recovery: dict[str, object] | None = None


def validate_run_trace_events(events: tuple[dict[str, Any], ...]) -> RunTraceValidation:
    findings: list[dict[str, str]] = []
    for index, event in enumerate(events):
        event_type = str(event.get("type") or "")
        if event_type == "state_transition":
            _validate_state_transition(index, event, findings)
        if event_type == "tool_result":
            _validate_tool_result(index, event, findings)
    return RunTraceValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(item["code"] for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("run_trace", findings),
    )


def _validate_state_transition(index: int, event: dict[str, Any], findings: list[dict[str, str]]) -> None:
    _append_required_field_findings(
        findings,
        (
            _required_field_finding(index, event, "run_id", "RUNLOG_RUN_ID_MISSING"),
            _required_field_finding(index, event, "from", "RUNLOG_FROM_STATUS_MISSING"),
            _required_field_finding(index, event, "event", "RUNLOG_EVENT_MISSING"),
            _required_field_finding(index, event, "to", "RUNLOG_TO_STATUS_MISSING"),
        ),
    )
    contract = transition_contract(event.get("from"), event.get("to"))
    if not contract.allowed:
        findings.append(
            _finding(
                index,
                "STATE_TRANSITION_INVALID",
                f"{contract.from_status}->{contract.to_status}:{contract.reason}",
            )
        )


def _validate_tool_result(index: int, event: dict[str, Any], findings: list[dict[str, str]]) -> None:
    _append_required_field_findings(
        findings,
        (
            _required_field_finding(index, event, "run_id", "TOOL_TRACE_RUN_ID_MISSING"),
            _required_field_finding(index, event, "tool", "TOOL_TRACE_TOOL_MISSING"),
            _required_field_finding(index, event, "operation_id", "TOOL_TRACE_OPERATION_ID_MISSING"),
        ),
    )
    if not _has_duration(event):
        findings.append(_finding(index, "TOOL_TRACE_DURATION_MISSING", "duration_ms is required"))
    if event.get("ok") is False and not str(event.get("error_code") or ""):
        findings.append(_finding(index, "TOOL_TRACE_ERROR_CODE_MISSING", "failed tool_result needs error_code"))


def _required_field_finding(index: int, event: dict[str, Any], field: str, code: str) -> dict[str, str] | None:
    if str(event.get(field) or ""):
        return None
    return _finding(index, code, f"{field} is required")


def _append_required_field_findings(
    findings: list[dict[str, str]],
    candidates: tuple[dict[str, str] | None, ...],
) -> None:
    findings.extend(item for item in candidates if item is not None)


def _has_duration(event: dict[str, Any]) -> bool:
    value = event.get("duration_ms")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return value >= 0


def _finding(index: int, code: str, detail: str) -> dict[str, str]:
    return {"index": str(index), "code": code, "detail": detail}


__all__ = ["RunTraceValidation", "validate_run_trace_events"]
