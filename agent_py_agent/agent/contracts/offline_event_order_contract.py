
from __future__ import annotations

from typing import Any

from .offline_contract_report import OfflineContractValidation, finding, text, validation_report

TERMINAL_EVENTS = {"TASK_CANCELLED", "TASK_BLOCKED", "TASK_FAILED", "TASK_DONE"}
TOOL_RESULT_EVENTS = {"TOOL_OK", "TOOL_FAILED"}


def validate_event_order(
    *,
    run_id: str,
    events: tuple[dict[str, Any], ...],
) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    seen: set[str] = set()
    ignored: list[str] = []
    waiting_tool = False
    terminal = False
    for event in events:
        event_id = text(event.get("event_id"))
        if event_id and event_id in seen:
            ignored.append(event_id)
            continue
        if event_id:
            seen.add(event_id)
        event_type = text(event.get("type")).upper()
        if _run_scope_mismatch(run_id, event, findings):
            continue
        if _validate_late_event(terminal, event_type, findings):
            continue
        _validate_tool_order(waiting_tool, event_type, findings)
        waiting_tool = _next_waiting_tool(waiting_tool, event_type)
        terminal = terminal or event_type in TERMINAL_EVENTS
    return validation_report(findings, ignored_event_ids=tuple(ignored))


def _run_scope_mismatch(run_id: str, event: dict[str, Any], findings: list[dict[str, object]]) -> bool:
    if text(event.get("run_id")) != run_id:
        findings.append(finding("EVENT_RUN_ID_MISMATCH", {"event_id": text(event.get("event_id"))}))
        return True
    return False


def _validate_late_event(terminal: bool, event_type: str, findings: list[dict[str, object]]) -> bool:
    if not terminal:
        return False
    if event_type in TOOL_RESULT_EVENTS:
        findings.append(finding("EVENT_AFTER_TERMINAL"))
        return True
    if event_type == "APPROVED":
        findings.append(finding("APPROVAL_AFTER_BLOCKED"))
        return True
    return False


def _validate_tool_order(waiting_tool: bool, event_type: str, findings: list[dict[str, object]]) -> None:
    if event_type in TOOL_RESULT_EVENTS and not waiting_tool:
        findings.append(finding("EVENT_OUT_OF_ORDER"))


def _next_waiting_tool(waiting_tool: bool, event_type: str) -> bool:
    if event_type == "NEED_TOOL":
        return True
    if event_type in TOOL_RESULT_EVENTS:
        return False
    return waiting_tool


__all__ = ["validate_event_order"]
