from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from agent_py_agent.agent.contracts.state_machine import normalize_status

from .contract_fixture_runner import ContractFixtureResult, FixtureRunFacts, verify_contract_fixture

_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PLANNING": ("PENDING", "RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED", "PAUSED"),
    "PENDING": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED", "PAUSED"),
    "RUNNING": (
        "WAITING_FOR_TOOL",
        "WAITING_FOR_USER",
        "WAITING_FOR_CHILD",
        "VERIFYING",
        "BLOCKED",
        "FAILED",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "CANCELLED",
        "ABANDONED",
        "PAUSED",
    ),
    "WAITING_FOR_TOOL": ("RUNNING", "BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "CANCELLED"),
    "WAITING_FOR_USER": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "WAITING_FOR_CHILD": ("RUNNING", "BLOCKED", "FAILED", "CANCELLED", "ABANDONED"),
    "VERIFYING": ("DONE", "RUNNING", "BLOCKED", "FAILED", "CANCELLED"),
    "BLOCKED": ("REPAIRING", "TAKING_OVER", "RUNNING", "FAILED", "CANCELLED", "ABANDONED", "PAUSED"),
    # 暂停只能经显式恢复回到 PENDING（不直写 RUNNING），或被停止/放弃。
    "PAUSED": ("PENDING", "CANCELLED", "ABANDONED"),
    "REPAIRING": ("RUNNING", "VERIFYING", "BLOCKED", "FAILED", "CANCELLED"),
    "TAKING_OVER": ("RUNNING", "VERIFYING", "BLOCKED", "FAILED", "CANCELLED"),
    "FAILED": ("REPAIRING", "TAKING_OVER", "CANCELLED", "ABANDONED"),
    "TIMEOUT": ("REPAIRING", "TAKING_OVER", "FAILED", "CANCELLED", "ABANDONED"),
    "CHANNEL_ERROR": ("REPAIRING", "TAKING_OVER", "FAILED", "CANCELLED", "ABANDONED"),
    "DONE": (),
    "CANCELLED": (),
    "ABANDONED": (),
}


@dataclass(frozen=True)
class TransitionContract:
    allowed: bool
    from_status: str
    to_status: str
    reason: str
    required_condition: str


def allowed_next_statuses(status: object) -> tuple[str, ...]:
    return _TRANSITIONS.get(normalize_status(status), ())


def transition_contract(from_status: object, to_status: object) -> TransitionContract:
    source = normalize_status(from_status)
    target = normalize_status(to_status)
    if source == target:
        return TransitionContract(True, source, target, "same_state", "idempotent_snapshot_repeat")
    if target in allowed_next_statuses(source):
        return TransitionContract(True, source, target, "allowed_transition", _required_condition(source, target))
    if source not in _TRANSITIONS:
        return TransitionContract(False, source, target, "unknown_source_state", "source_state_must_be_registered")
    return TransitionContract(False, source, target, "disallowed_transition", _required_condition(source, target))


def first_invalid_transition(statuses: list[object]) -> TransitionContract | None:
    normalized = [normalize_status(item) for item in statuses if str(item or "").strip()]
    for current, nxt in zip(normalized, normalized[1:], strict=False):
        contract = transition_contract(current, nxt)
        if not contract.allowed:
            return contract
    return None


def _required_condition(source: str, target: str) -> str:
    if target == "PAUSED":
        return "explicit_task_control_pause"
    if source == "PAUSED" and target == "PENDING":
        return "explicit_resume_after_exact_cleanup"
    if target == "VERIFYING":
        return "artifact_written_and_pending_acceptance"
    if source == "VERIFYING" and target == "DONE":
        return "acceptance_passed"
    if target == "RUNNING" and source in {"BLOCKED", "REPAIRING", "TAKING_OVER"}:
        return "repair_or_takeover_resolved"
    if target in {"REPAIRING", "TAKING_OVER"}:
        return "terminal_or_blocked_failure_detected"
    if target == "WAITING_FOR_TOOL":
        return "tool_call_in_flight"
    if target == "WAITING_FOR_USER":
        return "approval_or_user_input_required"
    if target == "WAITING_FOR_CHILD":
        return "child_run_in_flight"
    if target in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return "runtime_or_contract_failure_detected"
    if target == "DONE":
        return "final_artifact_ready"
    if target in {"CANCELLED", "ABANDONED"}:
        return "stop_requested_or_run_retired"
    return "registered_lifecycle_transition"


@dataclass(frozen=True)
class TraceReplayResult:
    final_status: str
    blocked: bool
    block_reason: str
    contract_result: ContractFixtureResult
    replay_error_codes: tuple[str, ...] = ()
    runtime_issues: tuple[dict[str, object], ...] = ()
    state_snapshots: tuple[dict[str, object], ...] = ()
    closeout_snapshots: tuple[dict[str, object], ...] = ()
    acceptance_reports: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class _ReplayEventFacts:
    runtime_issues: tuple[dict[str, object], ...]
    state_snapshots: tuple[dict[str, object], ...]
    closeout_snapshots: tuple[dict[str, object], ...]
    acceptance_reports: tuple[dict[str, object], ...]


def replay_contract_trace(trace_path: Path, run_dir: Path) -> TraceReplayResult:
    events = _events(trace_path)
    contract = _contract(events, trace_path.parent)
    tool_trace = [event for event in events if event.get("type") == "tool_result"]
    replay_facts = _replay_facts(events)
    final_status = _final_status(events)
    block_reason = _repeated_failure_block_reason(tool_trace, _repeat_fail_threshold(events))
    replay_errors = _replay_error_codes(final_status, replay_facts)
    contract_result = verify_contract_fixture(
        run_dir,
        contract,
        FixtureRunFacts(
            tool_trace=tuple(_tool_trace_item(event) for event in tool_trace),
            final_status=final_status,
            runtime_issues=replay_facts.runtime_issues,
        ),
    )
    return TraceReplayResult(
        final_status=final_status,
        blocked=bool(block_reason),
        block_reason=block_reason,
        contract_result=contract_result,
        replay_error_codes=replay_errors,
        runtime_issues=replay_facts.runtime_issues,
        state_snapshots=replay_facts.state_snapshots,
        closeout_snapshots=replay_facts.closeout_snapshots,
        acceptance_reports=replay_facts.acceptance_reports,
    )


def _events(trace_path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _contract(events: list[dict[str, object]], trace_root: Path) -> dict[str, object]:
    for event in events:
        if event.get("type") != "contract_ref":
            continue
        return json.loads((trace_root / str(event.get("path") or "")).resolve().read_text(encoding="utf-8"))
    return {}


def _final_status(events: list[dict[str, object]]) -> str:
    for event in reversed(events):
        if event.get("type") == "final":
            return str(event.get("status") or "UNKNOWN")
    return "UNKNOWN"


def _tool_trace_item(event: dict[str, object]) -> dict[str, object]:
    return {
        "tool": str(event.get("tool") or ""),
        "operation_id": str(event.get("operation_id") or ""),
        "params": dict(event.get("params")) if isinstance(event.get("params"), dict) else {},
        "result": {
            "ok": bool(event.get("ok")),
            "error_code": str(event.get("error_code") or ""),
            "duration_ms": event.get("duration_ms"),
            "dry_run": bool(event.get("dry_run")),
            "mode": str(event.get("mode") or ""),
        },
    }


def _repeated_failure_block_reason(tool_trace: list[dict[str, object]], repeat_fail_threshold: int = 10) -> str:
    threshold = max(0, repeat_fail_threshold)
    if threshold == 0:
        return ""
    counts: dict[str, int] = {}
    for event in tool_trace:
        if bool(event.get("ok")):
            continue
        key = _tool_failure_key(event)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= threshold * 3:
            return "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
    return ""


def _repeat_fail_threshold(events: list[dict[str, object]]) -> int:
    for event in events:
        if event.get("type") != "runtime_config":
            continue
        try:
            return max(0, int(event.get("repeat_fail_threshold", 10)))
        except (TypeError, ValueError):
            return 10
    return 10


def _replay_error_codes(
    final_status: str,
    facts: _ReplayEventFacts,
) -> tuple[str, ...]:
    errors: list[str] = []
    if str(final_status).upper() == "SUCCEEDED":
        if _has_blocking_runtime_issue(facts.runtime_issues):
            errors.append("RUNTIME_ISSUE_FINAL_CONFLICT")
        if _last_status(facts.state_snapshots) in {"BLOCKED", "FAILED"}:
            errors.append("STATE_SNAPSHOT_FINAL_CONFLICT")
        if facts.closeout_snapshots and not bool(facts.closeout_snapshots[-1].get("ok")):
            errors.append("CLOSEOUT_SNAPSHOT_FINAL_CONFLICT")
        if facts.acceptance_reports and not bool(facts.acceptance_reports[-1].get("ok")):
            errors.append("ACCEPTANCE_REPORT_FINAL_CONFLICT")
    if _has_invalid_transition(facts.state_snapshots):
        errors.append("STATE_TRANSITION_SEQUENCE_CONFLICT")
    return tuple(errors)


def _last_status(state_snapshots: tuple[dict[str, object], ...]) -> str:
    if not state_snapshots:
        return ""
    return str(state_snapshots[-1].get("status") or "").upper()


def _has_blocking_runtime_issue(runtime_issues: tuple[dict[str, object], ...]) -> bool:
    for item in runtime_issues:
        if bool(item.get("blocks_success")):
            return True
        severity = str(item.get("severity") or "").strip().lower()
        if severity in {"hard", "error", "fatal"}:
            return True
    return False


def _replay_facts(events: list[dict[str, object]]) -> _ReplayEventFacts:
    return _ReplayEventFacts(
        runtime_issues=tuple(event for event in events if event.get("type") == "runtime_issue"),
        state_snapshots=tuple(event for event in events if event.get("type") == "state_snapshot"),
        closeout_snapshots=tuple(event for event in events if event.get("type") == "closeout_snapshot"),
        acceptance_reports=tuple(event for event in events if event.get("type") == "acceptance_report"),
    )


def _has_invalid_transition(state_snapshots: tuple[dict[str, object], ...]) -> bool:
    statuses = [item.get("status") for item in state_snapshots]
    return first_invalid_transition(statuses) is not None


def _tool_failure_key(event: dict[str, object]) -> str:
    payload = {
        "error_code": str(event.get("error_code") or ""),
        "params": event.get("params") if isinstance(event.get("params"), dict) else {},
        "tool": str(event.get("tool") or ""),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
