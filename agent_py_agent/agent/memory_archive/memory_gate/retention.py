
from __future__ import annotations

"""retention planning for run-local memory gate queues.

Human version:
Retention here is deliberately conservative. It can compact the active review
queue, but the full candidate and decision logs stay in place for audit and
takeover. A dry run writes only a report; apply writes an action ledger too.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...common.json_io import (
    append_jsonl_records,
    read_json_object,
    write_json_object,
    write_jsonl_records,
)
from . import memory_gate_paths
from .candidates import memory_gate_review_queue_records, read_memory_gate_jsonl


@dataclass(frozen=True)
class MemoryGateRetentionRequest:
    """Controls whether retention only previews or updates active queue files."""

    apply: bool = False
    now: float | None = None


@dataclass(frozen=True)
class MemoryGateRetentionResult:
    """Retention report paths and counters for one agent run."""

    report: dict[str, object]
    report_json: Path
    actions_jsonl: Path


def run_memory_gate_retention(
    agent_run_workspace_root: Path,
    request: MemoryGateRetentionRequest,
) -> MemoryGateRetentionResult:
    """Plan or apply conservative retention for a run-local memory gate."""

    paths = memory_gate_paths(agent_run_workspace_root)
    paths.gate_dir.mkdir(parents=True, exist_ok=True)
    candidates = read_memory_gate_jsonl(paths.candidates_jsonl)
    closed = [_retention_action(item, request) for item in candidates if _is_closed_candidate(item)]
    closed_ids = {str(item["candidate_id"]) for item in closed}
    active_candidates = [item for item in candidates if str(item.get("candidate_id") or "") not in closed_ids]
    report = _report_payload(candidates, closed, request)
    report_json = paths.gate_dir / "retention_report.json"
    actions_jsonl = paths.gate_dir / "retention_actions.jsonl"
    write_json_object(report_json, report, sort_keys=False)
    if request.apply:
        write_jsonl_records(paths.review_queue_jsonl, memory_gate_review_queue_records(active_candidates))
        append_jsonl_records(actions_jsonl, closed)
        _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", report, report_json, actions_jsonl)
    return MemoryGateRetentionResult(report=report, report_json=report_json, actions_jsonl=actions_jsonl)


def _is_closed_candidate(candidate: dict[str, object]) -> bool:
    status = str(candidate.get("review_status") or "")
    promotion = str(candidate.get("promotion_status") or "")
    return status == "rejected" or promotion in {"promoted_to_memory", "skill_draft_created"}


def _retention_action(
    candidate: dict[str, object],
    request: MemoryGateRetentionRequest,
) -> dict[str, object]:
    return {
        "version": 1,
        "action": "remove_from_active_review_queue",
        "apply": request.apply,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_type": candidate.get("candidate_type", ""),
        "review_status": candidate.get("review_status", ""),
        "promotion_status": candidate.get("promotion_status", ""),
        "reason": "closed_candidate_kept_in_audit_logs",
        "created_at": _utc_iso(request.now),
    }


def _report_payload(
    candidates: list[dict[str, object]],
    actions: list[dict[str, object]],
    request: MemoryGateRetentionRequest,
) -> dict[str, object]:
    return {
        "version": 1,
        "mode": "apply" if request.apply else "dry_run",
        "candidate_count": len(candidates),
        "planned_action_count": len(actions),
        "active_after_count": len(candidates) - len(actions),
        "actions": actions,
        "policy": "audit_logs_preserved_active_queue_compacted_only",
        "updated_at": _utc_iso(request.now),
    }


def _merge_checkpoint(
    checkpoint_path: Path,
    report: dict[str, object],
    report_json: Path,
    actions_jsonl: Path,
) -> None:
    checkpoint = read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate["retention"] = {
        "mode": report.get("mode", ""),
        "planned_action_count": report.get("planned_action_count", 0),
        "report_ref": str(report_json),
        "actions_ref": str(actions_jsonl),
        "destructive_delete": False,
    }
    checkpoint["memory_gate"] = memory_gate
    write_json_object(checkpoint_path, checkpoint, sort_keys=False)


def _utc_iso(value: float | None) -> str:
    timestamp = value if value is not None else datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateRetentionRequest",
    "MemoryGateRetentionResult",
    "run_memory_gate_retention",
]
