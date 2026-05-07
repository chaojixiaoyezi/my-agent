from __future__ import annotations

"""LLM: retention planning for run-local memory gate queues.

Human version:
Retention here is deliberately conservative. It can compact the active review
queue, but the full candidate and decision logs stay in place for audit and
takeover. A dry run writes only a report; apply writes an action ledger too.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .memory_gate import memory_gate_paths
from .memory_gate_candidates import memory_gate_review_queue_records, read_memory_gate_jsonl


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
    _write_json(report_json, report)
    if request.apply:
        _write_jsonl(paths.review_queue_jsonl, memory_gate_review_queue_records(active_candidates))
        _append_jsonl_many(actions_jsonl, closed)
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
    checkpoint = _read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate["retention"] = {
        "mode": report.get("mode", ""),
        "planned_action_count": report.get("planned_action_count", 0),
        "report_ref": str(report_json),
        "actions_ref": str(actions_jsonl),
        "destructive_delete": False,
    }
    checkpoint["memory_gate"] = memory_gate
    _write_json(checkpoint_path, checkpoint)


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


def _append_jsonl_many(path: Path, records: list[dict[str, object]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _utc_iso(value: float | None) -> str:
    timestamp = value if value is not None else datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateRetentionRequest",
    "MemoryGateRetentionResult",
    "run_memory_gate_retention",
]
