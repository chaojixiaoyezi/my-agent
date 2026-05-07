from __future__ import annotations

"""LLM: review gate files for task-local memory and skill-spark candidates.

Human version:
Subagents can produce useful lessons and findings, but those facts must not
jump straight into main long-term memory or formal skills. This module writes a
small run-local gate queue so a parent/verifier can review evidence, scope, and
limits before any later promotion workflow.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_gate_candidates import (
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
    # LLM: candidate extraction is split out so this adapter stays below code-size guardrails.
    candidates = build_memory_gate_candidates(task, now=now, existing_candidates_jsonl=paths.candidates_jsonl)
    _write_jsonl(paths.candidates_jsonl, candidates)
    _write_jsonl(paths.review_queue_jsonl, memory_gate_review_queue_records(candidates))
    _write_json(paths.skill_spark_gate_json, _gate_summary(task, paths, candidates, now))
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
        },
        "updated_at": _utc_iso(now),
    }


def _merge_checkpoint(checkpoint_path: Path, paths: MemoryGateResult, candidates: list[dict[str, object]]) -> None:
    checkpoint = _read_json_object(checkpoint_path)
    checkpoint["memory_gate"] = {
        "status": "review_required",
        "candidate_count": len(candidates),
        "candidates_ref": str(paths.candidates_jsonl),
        "review_queue_ref": str(paths.review_queue_jsonl),
        "decisions_ref": str(paths.decisions_jsonl),
        "skill_spark_gate_ref": str(paths.skill_spark_gate_json),
        "auto_promote": False,
    }
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


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateResult",
    "list_memory_gate_candidates",
    "memory_gate_paths",
    "sync_agent_run_memory_gate",
]
