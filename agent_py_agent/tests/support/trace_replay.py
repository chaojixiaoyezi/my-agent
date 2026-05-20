from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .contract_fixture_runner import ContractFixtureResult, FixtureRunFacts, verify_contract_fixture


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
    block_reason = _repeated_failure_block_reason(tool_trace)
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
        "params": dict(event.get("params")) if isinstance(event.get("params"), dict) else {},
        "result": {
            "ok": bool(event.get("ok")),
            "error_code": str(event.get("error_code") or ""),
        },
    }


def _repeated_failure_block_reason(tool_trace: list[dict[str, object]]) -> str:
    counts: dict[str, int] = {}
    for event in tool_trace:
        if bool(event.get("ok")):
            continue
        key = _tool_failure_key(event)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= 3:
            return "TOOL_REPEATED_EXACT_FAILURE"
    return ""


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


def _tool_failure_key(event: dict[str, object]) -> str:
    payload = {
        "error_code": str(event.get("error_code") or ""),
        "params": event.get("params") if isinstance(event.get("params"), dict) else {},
        "tool": str(event.get("tool") or ""),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
