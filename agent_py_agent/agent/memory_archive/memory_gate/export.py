
from __future__ import annotations

"""explicit exports from reviewed run-local candidates.

Human version:
Review approval is not promotion. This module is the opt-in promotion step: it
exports approved memory candidates to the main JSONL memory, or writes approved
skill sparks as draft files inside the run-local gate area unless told otherwise.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...common.json_io import (
    append_jsonl_records,
    read_json_object,
    write_json_object,
    write_jsonl_records,
)
from ...common.path_segments import safe_path_segment
from ...memory_store.jsonl import JsonlMemory
from . import memory_gate_paths
from .candidates import memory_gate_review_queue_records, read_memory_gate_jsonl


@dataclass(frozen=True)
class MemoryGateExportRequest:
    """Export selector for one or more approved gate candidates."""

    candidate_id: str = ""
    reviewer: str = "parent"
    now: float | None = None
    memory_path: Path | str | None = None
    output_dir: Path | str | None = None


@dataclass(frozen=True)
class MemoryGateExportResult:
    """Export result for memory or skill draft promotion."""

    exported_count: int
    skipped_count: int
    exports_jsonl: Path
    exported: list[dict[str, object]]
    skipped: list[dict[str, object]]


def export_approved_memory_candidates(
    agent_run_workspace_root: Path,
    request: MemoryGateExportRequest,
) -> MemoryGateExportResult:
    """Write approved memory candidates into the main JSONL memory store."""

    paths = memory_gate_paths(agent_run_workspace_root)
    candidates = read_memory_gate_jsonl(paths.candidates_jsonl)
    exported, skipped = _partition_exportable(candidates, request, "approved_for_memory_export")
    memory_path = _required_path(request.memory_path, "memory_path")
    memory = JsonlMemory(memory_path)
    export_rows = [_memory_export_row(item, request, memory.path) for item in exported]
    for row in export_rows:
        item = row["memory_item"]
        memory.add(
            "system",
            json.dumps(item, ensure_ascii=False, sort_keys=True),
            kind="subagent_memory_candidate",
            tags=row["tags"],
            attributes={
                "origin": "reviewed",
                "evidence_refs": list(item.get("evidence_refs", []) or []),
                "subject_key": f"memory_gate:{row.get('candidate_id', '')}",
            },
        )
    updated = _mark_exported(candidates, export_rows, "promoted_to_memory", "memory_export_ref")
    _write_gate_after_export(agent_run_workspace_root, updated, export_rows, paths.exports_jsonl)
    return MemoryGateExportResult(len(export_rows), len(skipped), paths.exports_jsonl, export_rows, skipped)


def export_approved_skill_sparks(
    agent_run_workspace_root: Path,
    request: MemoryGateExportRequest,
) -> MemoryGateExportResult:
    """Write approved skill candidates as draft files, not installed skills."""

    paths = memory_gate_paths(agent_run_workspace_root)
    candidates = read_memory_gate_jsonl(paths.candidates_jsonl)
    exported, skipped = _partition_exportable(candidates, request, "approved_for_skill_export")
    draft_root = Path(request.output_dir) if request.output_dir else (paths.gate_dir / "skill_drafts")
    export_rows = [_skill_export_row(item, request, draft_root) for item in exported]
    for row in export_rows:
        _write_skill_draft(Path(str(row["draft_path"])), row)
    updated = _mark_exported(candidates, export_rows, "skill_draft_created", "skill_draft_ref")
    _write_gate_after_export(agent_run_workspace_root, updated, export_rows, paths.exports_jsonl)
    return MemoryGateExportResult(len(export_rows), len(skipped), paths.exports_jsonl, export_rows, skipped)


def _partition_exportable(
    candidates: list[dict[str, object]],
    request: MemoryGateExportRequest,
    required_status: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    selected = [item for item in candidates if _matches_candidate(item, request.candidate_id)]
    exported = [item for item in selected if item.get("promotion_status") == required_status]
    skipped = [item for item in selected if item.get("promotion_status") != required_status]
    return exported, skipped


def _matches_candidate(candidate: dict[str, object], candidate_id: str) -> bool:
    return not candidate_id or str(candidate.get("candidate_id") or "") == candidate_id


def _memory_export_row(
    candidate: dict[str, object],
    request: MemoryGateExportRequest,
    memory_path: Path,
) -> dict[str, object]:
    now = _utc_iso(request.now)
    memory_item = {
        "scope": candidate.get("applicability_scope", ""),
        "content": candidate.get("content", ""),
        "source": "subagent_memory_gate",
        "source_candidate_id": candidate.get("candidate_id", ""),
        "source_run_id": candidate.get("run_id", ""),
        "source_task_id": candidate.get("task_id", ""),
        "evidence_refs": candidate.get("evidence_refs", []),
        "artifact_refs": candidate.get("artifact_refs", []),
        "status": "active",
        "confidence": "reviewed",
        "retention": "explicit_review_required_before_deletion",
        "exported_at": now,
    }
    return {
        "version": 1,
        "export_type": "memory",
        "candidate_id": candidate.get("candidate_id", ""),
        "reviewer": request.reviewer,
        "exported_at": now,
        "memory_path": str(memory_path),
        "tags": ["subagent", "memory_gate", str(candidate.get("run_id") or "")],
        "memory_item": memory_item,
    }


def _skill_export_row(
    candidate: dict[str, object],
    request: MemoryGateExportRequest,
    draft_root: Path,
) -> dict[str, object]:
    now = _utc_iso(request.now)
    candidate_id = str(candidate.get("candidate_id") or "candidate")
    draft_path = draft_root / safe_path_segment(candidate_id, default="candidate", replacement="_") / "SKILL_DRAFT.md"
    return {
        "version": 1,
        "export_type": "skill_draft",
        "candidate_id": candidate_id,
        "candidate_type": candidate.get("candidate_type", ""),
        "reviewer": request.reviewer,
        "exported_at": now,
        "draft_path": str(draft_path),
        "content": candidate.get("content", ""),
        "applicability_scope": candidate.get("applicability_scope", ""),
        "evidence_refs": candidate.get("evidence_refs", []),
        "artifact_refs": candidate.get("artifact_refs", []),
        "limits": candidate.get("missing_requirements", []),
        "install_status": "draft_only_not_installed",
    }


def _write_skill_draft(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Skill Draft",
        "",
        f"- candidate_id: {row.get('candidate_id', '')}",
        f"- exported_at: {row.get('exported_at', '')}",
        f"- install_status: {row.get('install_status', '')}",
        "",
        "## Trigger",
        str(row.get("applicability_scope", "")),
        "",
        "## Reusable Step",
        str(row.get("content", "")),
        "",
        "## Evidence Refs",
        *[f"- {item}" for item in list(row.get("evidence_refs", []) or [])],
        "",
        "## Artifact Refs",
        *[f"- {item}" for item in list(row.get("artifact_refs", []) or [])],
        "",
        "## Limits Or Counterexamples",
        *[f"- {item}" for item in list(row.get("limits", []) or [])],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _mark_exported(
    candidates: list[dict[str, object]],
    export_rows: list[dict[str, object]],
    promotion_status: str,
    ref_key: str,
) -> list[dict[str, object]]:
    refs = {str(row["candidate_id"]): row for row in export_rows}
    updated: list[dict[str, object]] = []
    for candidate in candidates:
        item = dict(candidate)
        row = refs.get(str(item.get("candidate_id") or ""))
        if row:
            item["promotion_status"] = promotion_status
            item["promoted_at"] = row["exported_at"]
            item[ref_key] = str(row.get("draft_path") or row.get("memory_path") or "")
        updated.append(item)
    return updated


def _write_gate_after_export(
    agent_run_workspace_root: Path,
    candidates: list[dict[str, object]],
    rows: list[dict[str, object]],
    exports_jsonl: Path,
) -> None:
    paths = memory_gate_paths(agent_run_workspace_root)
    write_jsonl_records(paths.candidates_jsonl, candidates)
    write_jsonl_records(paths.review_queue_jsonl, memory_gate_review_queue_records(candidates))
    append_jsonl_records(exports_jsonl, rows)
    _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", paths.exports_jsonl, rows)


def _merge_checkpoint(checkpoint_path: Path, exports_jsonl: Path, rows: list[dict[str, object]]) -> None:
    checkpoint = read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate["exports_ref"] = str(exports_jsonl)
    memory_gate["last_export_count"] = len(rows)
    memory_gate["auto_promote"] = False
    checkpoint["memory_gate"] = memory_gate
    write_json_object(checkpoint_path, checkpoint, sort_keys=False)


def _required_path(value: Path | str | None, field_name: str) -> Path:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field_name} is required for this memory gate export")
    return Path(value)


def _utc_iso(value: float | None) -> str:
    timestamp = value if value is not None else datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateExportRequest",
    "MemoryGateExportResult",
    "export_approved_memory_candidates",
    "export_approved_skill_sparks",
]
