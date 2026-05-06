from __future__ import annotations

"""LLM: rescue/escalation annotations for due-check action plans."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import DueCheckIssue


def rescue_fields_for_issue(issue: DueCheckIssue, action: str) -> dict[str, object]:
    """Build non-mutating rescue metadata for one due-check issue."""
    strategy, target = _strategy_and_target(issue.kind, action)
    return {
        "rescue_trigger": issue.kind,
        "rescue_strategy": strategy,
        "escalation_target": target,
        "rescue_context_refs": _context_refs(issue),
    }


def merge_rescue_fields(item, issue: DueCheckIssue, action: str) -> None:
    """Merge another due-check issue into an existing action's rescue metadata."""
    fields = rescue_fields_for_issue(issue, action)
    triggers = [value for value in item.rescue_trigger.split(",") if value]
    trigger = str(fields["rescue_trigger"])
    if trigger not in triggers:
        triggers.append(trigger)
    item.rescue_trigger = ",".join(triggers)
    item.rescue_context_refs = _merge_refs(
        item.rescue_context_refs,
        [str(value) for value in fields["rescue_context_refs"]],
    )
    if not item.rescue_strategy:
        item.rescue_strategy = str(fields["rescue_strategy"])
    if not item.escalation_target or issue.severity == "P0":
        item.escalation_target = str(fields["escalation_target"])


def action_rescue_record_fields(action) -> dict[str, object]:
    """Copy rescue metadata from action plan into apply records."""
    return {
        "rescue_trigger": getattr(action, "rescue_trigger", ""),
        "rescue_strategy": getattr(action, "rescue_strategy", ""),
        "escalation_target": getattr(action, "escalation_target", ""),
        "rescue_context_refs": list(getattr(action, "rescue_context_refs", []) or []),
    }


def _strategy_and_target(kind: str, action: str) -> tuple[str, str]:
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_shrink_scope_before_retry", "parent"
    if kind in {"status_failed", "status_blocked"}:
        return "inspect_failure_then_rescue_or_escalate", "parent"
    if kind in {"channel_broken", "channel_probe_missing", "status_channel_error"}:
        return "repair_channel_before_retry", "runtime_owner"
    if kind == "open_capability_request":
        return "route_capability_request_before_retry", "capability_router"
    if kind == "open_capability_gap":
        return "escalate_capability_gap_for_tooling_or_learning", "capability_owner"
    if kind == "fake_done_risk":
        return "reopen_and_request_missing_evidence", "parent"
    if kind == "missing_work_order_files":
        return "repair_work_order_before_any_retry", "parent"
    return action or "inspect_manually", "parent"


def _context_refs(issue: DueCheckIssue) -> list[str]:
    refs = [issue.task_dir]
    refs.extend([
        f"status:{issue.status}",
        f"severity:{issue.severity}",
        f"age_seconds:{issue.age_seconds:.0f}",
        f"stale_seconds:{issue.stale_seconds:.0f}",
    ])
    if issue.open_request_count:
        refs.append(f"open_capability_requests:{issue.open_request_count}")
    if issue.open_gap_count:
        refs.append(f"open_capability_gaps:{issue.open_gap_count}")
    return [ref for ref in refs if ref]


def _merge_refs(existing: list[str], incoming: list[str]) -> list[str]:
    refs = list(existing)
    for ref in incoming:
        if ref and ref not in refs:
            refs.append(ref)
    return refs
