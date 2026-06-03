
from __future__ import annotations

"""review gate files for task-local memory and skill-spark candidates.

Human version:
Subagents can produce useful lessons and findings, but those facts must not
jump straight into main long-term memory or formal skills. This module writes a
small run-local gate queue so a parent/verifier can review evidence, scope, and
limits before any later promotion workflow.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...common.json_io import read_json_object, write_json_object, write_jsonl_records
from .candidates import (
    build_memory_gate_candidates,
    memory_gate_review_queue_records,
    read_memory_gate_jsonl,
)


@dataclass(frozen=True)
class MemoryGateResult:
    """Concrete files for one run-local memory promotion gate."""

    gate_dir: Path
    candidates_jsonl: Path
    review_queue_jsonl: Path
    decisions_jsonl: Path
    exports_jsonl: Path
    skill_spark_gate_json: Path


def sync_agent_run_memory_gate(
    task: Any,
    *,
    agent_run_workspace_root: Path,
    now: float,
) -> MemoryGateResult:
    """Write gated memory/skill candidate records for one subagent run."""

    paths = memory_gate_paths(agent_run_workspace_root)
    paths.gate_dir.mkdir(parents=True, exist_ok=True)
    candidates = build_memory_gate_candidates(task, now=now, existing_candidates_jsonl=paths.candidates_jsonl)
    write_jsonl_records(paths.candidates_jsonl, candidates)
    write_jsonl_records(paths.review_queue_jsonl, memory_gate_review_queue_records(candidates))
    write_json_object(paths.skill_spark_gate_json, _gate_summary(task, paths, candidates, now), sort_keys=False)
    _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", paths, candidates)
    return paths


def memory_gate_paths(agent_run_workspace_root: Path) -> MemoryGateResult:
    """Return memory gate paths without writing files."""

    gate_dir = agent_run_workspace_root / "memory_gate"
    return MemoryGateResult(
        gate_dir=gate_dir,
        candidates_jsonl=gate_dir / "candidates.jsonl",
        review_queue_jsonl=gate_dir / "review_queue.jsonl",
        decisions_jsonl=gate_dir / "decisions.jsonl",
        exports_jsonl=gate_dir / "exports.jsonl",
        skill_spark_gate_json=gate_dir / "skill_spark_gate.json",
    )


def list_memory_gate_candidates(agent_run_workspace_root: Path) -> list[dict[str, object]]:
    """Read current memory-gate candidates for one agent run workspace."""

    return read_memory_gate_jsonl(memory_gate_paths(agent_run_workspace_root).candidates_jsonl)


def _gate_summary(
    task: Any,
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
    now: float,
) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": str(getattr(task, "root_id", "") or getattr(task, "id", "task")),
        "run_id": str(getattr(task, "id", "")),
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
        "updated_at": _utc_iso(now),
    }


def _merge_checkpoint(checkpoint_path: Path, paths: MemoryGateResult, candidates: list[dict[str, object]]) -> None:
    checkpoint = read_json_object(checkpoint_path)
    checkpoint["memory_gate"] = {
        "status": "review_required",
        "candidate_count": len(candidates),
        "candidates_ref": str(paths.candidates_jsonl),
        "review_queue_ref": str(paths.review_queue_jsonl),
        "decisions_ref": str(paths.decisions_jsonl),
        "exports_ref": str(paths.exports_jsonl),
        "skill_spark_gate_ref": str(paths.skill_spark_gate_json),
        "auto_promote": False,
    }
    write_json_object(checkpoint_path, checkpoint, sort_keys=False)


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateResult",
    "list_memory_gate_candidates",
    "memory_gate_paths",
    "sync_agent_run_memory_gate",
]
