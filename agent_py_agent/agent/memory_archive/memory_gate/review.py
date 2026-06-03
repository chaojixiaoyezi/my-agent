
from __future__ import annotations

"""explicit review write-back for run-local memory gate candidates.

Human version:
This module records reviewer decisions in `memory_gate/decisions.jsonl`. An
approval here never writes main memory or creates a formal skill; it only marks
the candidate as eligible for a later explicit export workflow.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...common.json_io import (
    append_jsonl_records,
    read_json_object,
    read_jsonl_objects,
    write_json_object,
    write_jsonl_records,
)
from . import MemoryGateResult, memory_gate_paths


@dataclass(frozen=True)
class MemoryGateReviewRequest:
    """Review request fields bundled to avoid wide helper signatures."""

    candidate_id: str
    decision: str
    reviewer: str = "parent"
    note: str = ""
    now: float | None = None


@dataclass(frozen=True)
class MemoryGateReviewResult:
    """Review write-back result for one gated candidate."""

    candidate: dict[str, object]
    decisions_jsonl: Path
    skill_spark_gate_json: Path


@dataclass(frozen=True)
class _ReviewFieldsParams:
    candidate: dict[str, object]
    decision: str
    request: MemoryGateReviewRequest
    reviewed_at: str


def record_memory_gate_review(
    agent_run_workspace_root: Path,
    request: MemoryGateReviewRequest,
) -> MemoryGateReviewResult:
    """Record an explicit review decision without exporting memory or skills."""

    paths = memory_gate_paths(agent_run_workspace_root)
    candidates = read_jsonl_objects(paths.candidates_jsonl)
    updated, reviewed = _apply_review_decision(candidates, request)
    write_jsonl_records(paths.candidates_jsonl, updated)
    write_jsonl_records(paths.review_queue_jsonl, _review_queue_records(updated))
    append_jsonl_records(paths.decisions_jsonl, [_decision_record(reviewed)])
    write_json_object(paths.skill_spark_gate_json, _review_gate_summary(paths, updated, reviewed), sort_keys=False)
    _merge_checkpoint_from_review(agent_run_workspace_root / "checkpoint.json", paths, updated)
    return MemoryGateReviewResult(reviewed, paths.decisions_jsonl, paths.skill_spark_gate_json)


def _apply_review_decision(
    candidates: list[dict[str, object]],
    request: MemoryGateReviewRequest,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    normalized = _normalize_decision(request.decision)
    reviewed_at = _utc_iso(request.now or datetime.now(timezone.utc).timestamp())
    updated: list[dict[str, object]] = []
    reviewed: dict[str, object] | None = None
    for candidate in candidates:
        item = dict(candidate)
        if str(item.get("candidate_id") or "") == request.candidate_id:
            _apply_review_fields(_ReviewFieldsParams(item, normalized, request, reviewed_at))
            reviewed = item
        updated.append(item)
    if reviewed is None:
        raise FileNotFoundError(request.candidate_id)
    return updated, reviewed


def _apply_review_fields(params: _ReviewFieldsParams) -> None:
    status, promotion_status, requires_more_review = _decision_status(params.decision)
    candidate = params.candidate
    candidate["review_decision"] = params.decision
    candidate["review_status"] = status
    candidate["promotion_status"] = promotion_status
    candidate["review_required"] = requires_more_review
    candidate["gate_status"] = "needs_review" if requires_more_review else "reviewed"
    candidate["reviewer"] = params.request.reviewer or "parent"
    candidate["review_note"] = params.request.note
    candidate["reviewed_at"] = params.reviewed_at


def _decision_status(decision: str) -> tuple[str, str, bool]:
    if decision == "approve_memory":
        return "approved", "approved_for_memory_export", False
    if decision == "approve_skill":
        return "approved", "approved_for_skill_export", False
    if decision == "reject":
        return "rejected", "rejected", False
    return "needs_evidence", "not_promoted", True


def _normalize_decision(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"approve_memory", "approve_skill", "reject", "needs_evidence"}:
        return normalized
    raise ValueError(f"unsupported memory gate decision: {value}")


def _decision_record(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_type": candidate.get("candidate_type", ""),
        "run_id": candidate.get("run_id", ""),
        "task_id": candidate.get("task_id", ""),
        "review_decision": candidate.get("review_decision", ""),
        "review_status": candidate.get("review_status", ""),
        "promotion_status": candidate.get("promotion_status", ""),
        "reviewer": candidate.get("reviewer", ""),
        "review_note": candidate.get("review_note", ""),
        "reviewed_at": candidate.get("reviewed_at", ""),
        "auto_promote": False,
    }


def _review_gate_summary(
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
    reviewed: dict[str, object],
) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": reviewed.get("task_id", ""),
        "run_id": reviewed.get("run_id", ""),
        "candidate_count": len(candidates),
        "review_required_count": sum(1 for item in candidates if item.get("review_required")),
        "approved_count": sum(1 for item in candidates if item.get("review_status") == "approved"),
        "rejected_count": sum(1 for item in candidates if item.get("review_status") == "rejected"),
        "promoted_count": 0,
        "promotion_policy": "never_auto_promote",
        "refs": {
            "candidates": str(paths.candidates_jsonl),
            "review_queue": str(paths.review_queue_jsonl),
            "decisions": str(paths.decisions_jsonl),
            "exports": str(paths.exports_jsonl),
        },
        "updated_at": str(reviewed.get("reviewed_at", "")),
    }


def _review_queue_records(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "version": 1,
            "candidate_id": item["candidate_id"],
            "candidate_type": item["candidate_type"],
            "gate_status": item["gate_status"],
            "promotion_status": item["promotion_status"],
            "review_status": item.get("review_status", "pending"),
            "review_required": item["review_required"],
            "missing_requirements": list(item.get("missing_requirements", []) or []),
            "source_refs": dict(item.get("source_refs", {}) or {}),
        }
        for item in candidates
    ]


def _merge_checkpoint_from_review(
    checkpoint_path: Path,
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
) -> None:
    checkpoint = read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate.update(
        {
            "status": "reviewed" if any(not item.get("review_required") for item in candidates) else "review_required",
            "candidate_count": len(candidates),
            "candidates_ref": str(paths.candidates_jsonl),
            "review_queue_ref": str(paths.review_queue_jsonl),
            "decisions_ref": str(paths.decisions_jsonl),
            "exports_ref": str(paths.exports_jsonl),
            "skill_spark_gate_ref": str(paths.skill_spark_gate_json),
            "auto_promote": False,
        },
    )
    checkpoint["memory_gate"] = memory_gate
    write_json_object(checkpoint_path, checkpoint, sort_keys=False)


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = ["MemoryGateReviewRequest", "MemoryGateReviewResult", "record_memory_gate_review"]
