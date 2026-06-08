
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..recovery_actions import RecoveryAction
from ..state_machine_transitions import transition_contract
from .models import GateDecision, GateFinding

_TERMINAL_STATUSES = {"DONE", "CANCELLED", "ABANDONED"}
_ACTION_EVENTS = {"dispatch", "tool_call", "execute_tool"}
_TOOL_RESULT_EVENTS = {"tool_result", "tool_completed"}
_ACTIVE_STATUSES = {"RUNNING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER"}


@dataclass(frozen=True)
class StateEventLedgerSnapshot:
    run_id: str
    current_status: str
    transitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()
    next_action: str = ""
    lease_status: str = ""


def evaluate_state_event_ledger_gate(snapshot: StateEventLedgerSnapshot | Mapping[str, object]) -> GateDecision:
    item = _ledger_snapshot(snapshot)
    findings: list[GateFinding] = []
    events = [_event_dict(event) for event in item.events if isinstance(event, Mapping)]
    event_ids = {str(event.get("event_id") or "").strip() for event in events if str(event.get("event_id") or "").strip()}
    _collect_run_mismatch_findings(item, events, findings)
    _collect_transition_findings(item, event_ids, findings)
    _collect_terminal_action_findings(item, findings)
    _collect_lease_findings(item, events, findings)
    _collect_duplicate_action_findings(item, events, findings)
    if findings:
        return GateDecision(
            "state_event_ledger",
            "DENY",
            False,
            tuple(findings),
            RecoveryAction.REPAIR.value,
            {},
        )
    return GateDecision.allow(
        "state_event_ledger",
        evidence={
            "run_id": item.run_id,
            "current_status": str(item.current_status or "").strip().upper(),
            "event_count": len(events),
            "transition_count": len(item.transitions),
        },
    )


def _collect_run_mismatch_findings(
    snapshot: StateEventLedgerSnapshot,
    events: list[dict[str, Any]],
    findings: list[GateFinding],
) -> None:
    run_id = str(snapshot.run_id or "").strip()
    if not run_id:
        return
    for event in events:
        event_run_id = str(event.get("run_id") or "").strip()
        if event_run_id and event_run_id != run_id:
            findings.append(
                GateFinding(
                    "STATE_EVENT_LEDGER_RUN_MISMATCH",
                    evidence={"run_id": run_id, "event_run_id": event_run_id},
                )
            )
            return


def _collect_transition_findings(
    snapshot: StateEventLedgerSnapshot,
    event_ids: set[str],
    findings: list[GateFinding],
) -> None:
    for transition in snapshot.transitions:
        event_id = str(transition.get("event_id") or "").strip()
        if not event_id or event_id not in event_ids:
            findings.append(GateFinding("STATE_EVENT_LEDGER_EVENT_MISSING", evidence={"run_id": snapshot.run_id}))
            continue
        contract = transition_contract(transition.get("from_status"), transition.get("to_status"))
        if not contract.allowed:
            findings.append(
                GateFinding(
                    "STATE_EVENT_LEDGER_TRANSITION_DISALLOWED",
                    evidence={"from_status": contract.from_status, "to_status": contract.to_status},
                )
            )


def _collect_terminal_action_findings(snapshot: StateEventLedgerSnapshot, findings: list[GateFinding]) -> None:
    status = str(snapshot.current_status or "").strip()
    action = str(snapshot.next_action or "").strip()
    if status in _TERMINAL_STATUSES and action in _ACTION_EVENTS:
        findings.append(GateFinding("STATE_EVENT_LEDGER_TERMINAL_ACTION_BLOCKED", evidence={"status": status}))


def _collect_lease_findings(
    snapshot: StateEventLedgerSnapshot,
    events: list[dict[str, Any]],
    findings: list[GateFinding],
) -> None:
    if str(snapshot.lease_status or "").strip().upper() != "EXPIRED":
        return
    if any(str(event.get("event_type") or "").strip().lower() in _TOOL_RESULT_EVENTS for event in events):
        findings.append(GateFinding("STATE_EVENT_LEDGER_LEASE_EXPIRED_RESULT", evidence={"run_id": snapshot.run_id}))


def _collect_duplicate_action_findings(
    snapshot: StateEventLedgerSnapshot,
    events: list[dict[str, Any]],
    findings: list[GateFinding],
) -> None:
    if str(snapshot.current_status or "").strip().upper() not in _ACTIVE_STATUSES:
        return
    seen: set[str] = set()
    for event in events:
        if str(event.get("event_type") or "").strip().lower() not in _ACTION_EVENTS:
            continue
        operation_id = str(event.get("operation_id") or "").strip()
        if not operation_id:
            continue
        if operation_id in seen:
            findings.append(GateFinding("STATE_EVENT_LEDGER_DUPLICATE_ACTION", evidence={"operation_id": operation_id}))
            return
        seen.add(operation_id)


def _ledger_snapshot(value: StateEventLedgerSnapshot | Mapping[str, object]) -> StateEventLedgerSnapshot:
    if isinstance(value, StateEventLedgerSnapshot):
        return value
    transitions = value.get("transitions") if isinstance(value.get("transitions"), (list, tuple)) else ()
    events = value.get("events") if isinstance(value.get("events"), (list, tuple)) else ()
    return StateEventLedgerSnapshot(
        run_id=str(value.get("run_id") or ""),
        current_status=str(value.get("current_status") or value.get("status") or ""),
        transitions=transitions,
        events=events,
        next_action=str(value.get("next_action") or ""),
        lease_status=str(value.get("lease_status") or ""),
    )

def _event_dict(event: Mapping[object, object]) -> dict[str, Any]:
    return {str(key): value for key, value in event.items()}


__all__ = ["StateEventLedgerSnapshot", "evaluate_state_event_ledger_gate"]
