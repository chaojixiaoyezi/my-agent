
from __future__ import annotations

"""rescue/escalation annotations for due-check action plans."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ...common.value_parsing import sequence_strings
from ..policies import (
    ACTION_INSPECT_MANUALLY,
    RESCUE_POLICIES,
)
from .takeover.readiness import takeover_readiness_ref_order

if TYPE_CHECKING:
    from ..reports import DueCheckIssue


def rescue_fields_for_issue(issue: DueCheckIssue, action: str) -> dict[str, object]:
    """Build non-mutating rescue metadata for one due-check issue."""
    strategy, target = _strategy_and_target(issue.kind, action)
    refs = _context_refs(issue)
    return {
        "rescue_trigger": issue.kind,
        "rescue_strategy": strategy,
        "escalation_target": target,
        "rescue_context_refs": refs,
        "rescue_packet": _build_rescue_packet(issue, action, refs),
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
    _merge_rescue_packet(item, fields, issue)
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
        "rescue_packet": dict(getattr(action, "rescue_packet", {}) or {}),
    }


def _strategy_and_target(kind: str, action: str) -> tuple[str, str]:
    if policy := RESCUE_POLICIES.get(kind):
        return policy.strategy, policy.target
    return action or ACTION_INSPECT_MANUALLY, "parent"


def _context_refs(issue: DueCheckIssue) -> list[str]:
    refs = [issue.task_dir]
    refs.extend(_readiness_refs_for_issue(issue))
    refs.extend(str(ref) for ref in getattr(issue, "related_refs", []) or [])
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


def _readiness_refs_for_issue(issue: DueCheckIssue) -> list[str]:
    if not issue.task_dir:
        return []
    for path in _readiness_candidate_paths(Path(issue.task_dir)):
        if path.exists():
            return takeover_readiness_ref_order(str(path))
    return []


def _readiness_candidate_paths(task_dir: Path) -> list[Path]:
    candidates = [task_dir / "reports" / "takeover_readiness.json"]
    locator = task_dir / "task.json"
    try:
        payload = json.loads(locator.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return candidates
    agent_workspace = str(payload.get("agent_run_workspace_dir") or "").strip()
    if agent_workspace:
        candidates.insert(0, Path(agent_workspace) / "reports" / "takeover_readiness.json")
    return candidates


def _build_rescue_packet(
    issue: DueCheckIssue,
    action: str,
    refs: list[str],
) -> dict[str, object]:
    strategy, target = _strategy_and_target(issue.kind, action)
    return {
        "schema_name": "subagent_rescue_packet",
        "schema_version": 1,
        "run_id": issue.run_id,
        "action": action,
        "dedupe_key": f"{issue.run_id}:{action}",
        "issue_kinds": [issue.kind],
        "repeat_count": 1,
        "retry_policy": {
            "max_attempts": 1,
            "current_attempts": 0,
            "auto_retry": False,
        },
        "escalation": {
            "target": target,
            "strategy": strategy,
        },
        "manual_confirmation": {
            "required": True,
            "reason": "rescue_action_is_a_plan_not_auto_execution",
        },
        "recovery_entrypoints": _unique_strings(refs[1:] if refs and refs[0] == issue.task_dir else refs),
        "reads_artifact_bodies": False,
        "auto_execute": False,
        "packet_is_action_plan_index": True,
    }


def _merge_rescue_packet(item, fields: dict[str, object], issue: DueCheckIssue) -> None:
    incoming = fields.get("rescue_packet")
    if not isinstance(incoming, dict):
        return
    packet = dict(getattr(item, "rescue_packet", {}) or {})
    if not packet:
        item.rescue_packet = dict(incoming)
        return
    issue_kinds = _merge_refs(sequence_strings(packet.get("issue_kinds")), sequence_strings(incoming.get("issue_kinds")))
    packet["issue_kinds"] = issue_kinds
    packet["repeat_count"] = len(issue_kinds)
    packet["recovery_entrypoints"] = _merge_refs(
        sequence_strings(packet.get("recovery_entrypoints")),
        sequence_strings(incoming.get("recovery_entrypoints")),
    )
    if issue.severity == "P0":
        packet["escalation"] = incoming.get("escalation", packet.get("escalation", {}))
    item.rescue_packet = packet


def _merge_refs(existing: list[str], incoming: list[str]) -> list[str]:
    refs = list(existing)
    for ref in incoming:
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
