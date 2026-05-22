# LLM: State/event ledger gates keep lifecycle changes and async results tied to auditable events.
# 模块用途: 校验状态迁移、事件记录、lease 和重复调度，防止 DONE 继续跑或过期 worker 写成功。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..state_machine_transitions import transition_contract
from .models import GateDecision, GateFinding

_TERMINAL_STATUSES = {"DONE", "CANCELLED", "ABANDONED"}
_ACTION_EVENTS = {"dispatch", "tool_call", "execute_tool"}
_TOOL_RESULT_EVENTS = {"tool_result", "tool_completed"}
_ACTIVE_STATUSES = {"RUNNING", "WAITING_FOR_TOOL", "WAITING_FOR_CHILD", "WAITING_FOR_USER"}


# LLM: StateEventLedgerSnapshot is the state-control input consumed by the gate.
# 类用途: 保存一个 run 的当前状态、事件流水、迁移记录和 lease 状态。
@dataclass(frozen=True)
class StateEventLedgerSnapshot:
    run_id: str
    current_status: str
    transitions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()
    next_action: str = ""
    lease_status: str = ""


# LLM: evaluate_state_event_ledger_gate validates transitions and async event ordering.
# 函数用途: 保证状态变化有事件、迁移合法、终态不再 dispatch、过期 lease 不能提交工具结果。
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
        return GateDecision("state_event_ledger", "DENY", False, tuple(findings), "repair_or_recover_ledger", {})
    return GateDecision.allow(
        "state_event_ledger",
        evidence={
            "run_id": item.run_id,
            "current_status": str(item.current_status or "").strip().upper(),
            "event_count": len(events),
            "transition_count": len(item.transitions),
        },
    )


# LLM: _collect_run_mismatch_findings blocks stale async events from other runs.
# 函数用途: 事件带 run_id 时必须匹配当前 snapshot.run_id，防止旧 run 结果误投递。
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


# LLM: _collect_transition_findings requires every state change to have a matching event and legal hop.
# 函数用途: 检查 transitions 列表，不允许绕过共享状态机或缺少 event_id。
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


# LLM: _collect_terminal_action_findings blocks work from restarting after terminal states.
# 函数用途: DONE/CANCELLED/ABANDONED 后不能继续 dispatch 或执行工具。
def _collect_terminal_action_findings(snapshot: StateEventLedgerSnapshot, findings: list[GateFinding]) -> None:
    status = str(snapshot.current_status or "").strip().upper()
    action = str(snapshot.next_action or "").strip().lower()
    if status in _TERMINAL_STATUSES and action in _ACTION_EVENTS:
        findings.append(GateFinding("STATE_EVENT_LEDGER_TERMINAL_ACTION_BLOCKED", evidence={"status": status}))


# LLM: _collect_lease_findings rejects late tool results after worker lease expiry.
# 函数用途: lease 已过期时，不允许 tool_result 事件继续推进成功链路。
def _collect_lease_findings(
    snapshot: StateEventLedgerSnapshot,
    events: list[dict[str, Any]],
    findings: list[GateFinding],
) -> None:
    if str(snapshot.lease_status or "").strip().upper() != "EXPIRED":
        return
    if any(str(event.get("event_type") or "").strip().lower() in _TOOL_RESULT_EVENTS for event in events):
        findings.append(GateFinding("STATE_EVENT_LEDGER_LEASE_EXPIRED_RESULT", evidence={"run_id": snapshot.run_id}))


# LLM: _collect_duplicate_action_findings detects repeated dispatch/tool actions for active runs.
# 函数用途: 同 operation_id 在活跃状态下重复出现，说明调度或工具提交不具备幂等保护。
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


# LLM: _ledger_snapshot converts dict payloads into the public dataclass.
# 函数用途: 兼容 persisted JSON rows，但 gate 内部只处理 StateEventLedgerSnapshot。
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

# LLM: _event_dict normalizes persisted event mappings into plain string-key dicts.
# 函数用途: 兼容 JSON 行和 dataclass/mapping 输入，后续 gate 只读字符串键。
def _event_dict(event: Mapping[object, object]) -> dict[str, Any]:
    return {str(key): value for key, value in event.items()}


__all__ = ["StateEventLedgerSnapshot", "evaluate_state_event_ledger_gate"]
