from __future__ import annotations

"""LLM: candidate extraction helpers for run-local memory gates."""

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class _GateCandidateContext:
    """LLM: Bundles stable run identity so candidate builders stay small."""

    task_id: str
    run_id: str
    now: float


@dataclass(frozen=True)
class _GateRequirementContext:
    """LLM: Groups gate evidence so requirement checks stay below guardrail limits."""

    content: str
    evidence_refs: list[str]
    artifact_refs: list[str]
    finding_ids: list[str]
    scope: str


def build_memory_gate_candidates(
    task: Any,
    *,
    now: float,
    existing_candidates_jsonl: Path,
) -> list[dict[str, object]]:
    """Build candidate records while preserving previous review decisions."""

    context = _GateCandidateContext(
        task_id=str(getattr(task, "root_id", "") or getattr(task, "id", "task")),
        run_id=str(getattr(task, "id", "")),
        now=now,
    )
    existing = _existing_candidates(existing_candidates_jsonl)
    records = [_merge_existing_review_state(_candidate_payload(task, context, "skill_spark", item), existing) for item in _lesson_texts(task)]
    records.extend(
        _merge_existing_review_state(_candidate_payload(task, context, "memory_candidate", item), existing)
        for item in _finding_texts(task)
    )
    return sorted(records, key=lambda item: str(item["candidate_id"]))


def memory_gate_review_queue_records(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the compact review queue projection for candidate records."""

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


def read_memory_gate_jsonl(path: Path) -> list[dict[str, object]]:
    """Read a JSONL file, ignoring corrupt rows."""

    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _candidate_payload(
    task: Any,
    context: _GateCandidateContext,
    candidate_type: str,
    content: str,
) -> dict[str, object]:
    evidence_refs = _dedupe(_task_evidence_refs(task))
    artifact_refs = _dedupe(_task_artifact_refs(task))
    finding_ids = _dedupe(_finding_ids(task))
    scope = _scope(task)
    missing = _missing_requirements(_GateRequirementContext(content, evidence_refs, artifact_refs, finding_ids, scope))
    gate_status = "eligible_for_review" if not missing else "needs_review"
    return {
        "version": 1,
        "candidate_id": _candidate_id(context.run_id or context.task_id, candidate_type, content),
        "candidate_type": candidate_type,
        "task_id": context.task_id,
        "run_id": context.run_id or context.task_id,
        "content": content,
        "applicability_scope": scope,
        "gate_status": gate_status,
        "promotion_status": "not_promoted",
        "review_status": "pending",
        "review_required": True,
        "review_requirements": [
            "evidence_refs",
            "applicability_scope",
            "limits_or_counterexamples",
            "human_or_verifier_approval",
        ],
        "missing_requirements": missing,
        "evidence_refs": evidence_refs,
        "artifact_refs": artifact_refs,
        "finding_ids": finding_ids,
        "source_refs": _source_refs(task),
        "created_at": _utc_iso(context.now),
    }


def _lesson_texts(task: Any) -> list[str]:
    output = _read_json_object(Path(str(getattr(task, "output_json", ""))))
    lessons = output.get("lessons", [])
    if not isinstance(lessons, list):
        lessons = []
    attr_lessons = getattr(task, "attributes", {}).get("lessons", [])
    if isinstance(attr_lessons, list):
        lessons.extend(attr_lessons)
    return _dedupe(str(item).strip() for item in lessons if str(item).strip())


def _finding_texts(task: Any) -> list[str]:
    texts: list[str] = []
    for item in list(getattr(task, "findings", []) or []):
        payload = _record_payload(item)
        text = str(payload.get("claim") or payload.get("id") or "").strip()
        if text:
            texts.append(text)
    return _dedupe(texts)


def _missing_requirements(context: _GateRequirementContext) -> list[str]:
    missing: list[str] = []
    if not context.content.strip():
        missing.append("content")
    if not context.scope:
        missing.append("applicability_scope")
    if not context.evidence_refs and not context.artifact_refs and not context.finding_ids:
        missing.append("evidence_refs")
    # LLM: counterexamples stay explicit so later skill promotion cannot infer limits silently.
    missing.append("limits_or_counterexamples")
    missing.append("human_or_verifier_approval")
    return missing


def _task_evidence_refs(task: Any) -> list[str]:
    refs = list(getattr(task, "evidence_refs", []) or [])
    for item in list(getattr(task, "evidence_packets", []) or []):
        refs.extend(_record_list(item, "evidence_refs"))
        refs.extend(_record_list(item, "counter_evidence_refs"))
    for item in list(getattr(task, "findings", []) or []):
        refs.extend(_record_list(item, "evidence_refs"))
        refs.extend(_record_list(item, "counter_evidence_refs"))
        refs.extend(_record_list(item, "evidence_packet_ids"))
    return [str(item) for item in refs if str(item).strip()]


def _task_artifact_refs(task: Any) -> list[str]:
    refs = list(getattr(task, "artifact_refs", []) or [])
    for item in list(getattr(task, "evidence_packets", []) or []):
        refs.extend(_record_list(item, "artifact_refs"))
    return [str(item) for item in refs if str(item).strip()]


def _finding_ids(task: Any) -> list[str]:
    ids: list[str] = []
    for item in list(getattr(task, "findings", []) or []):
        value = str(_record_payload(item).get("id") or "").strip()
        if value:
            ids.append(value)
    return ids


def _source_refs(task: Any) -> dict[str, str]:
    return {
        "legacy_task_dir": str(getattr(task, "task_dir", "")),
        "output_json": str(getattr(task, "output_json", "")),
        "skill_sparks_file": str(getattr(task, "skill_sparks_file", "")),
        "status_report": str(getattr(task, "status_report_json", "")),
        "checkpoint": str(getattr(task, "checkpoint_json", "")),
    }


def _existing_candidates(path: Path) -> dict[str, dict[str, object]]:
    return {str(item.get("candidate_id") or ""): item for item in read_memory_gate_jsonl(path) if item.get("candidate_id")}


def _merge_existing_review_state(
    candidate: dict[str, object],
    existing_by_id: dict[str, dict[str, object]],
) -> dict[str, object]:
    previous = existing_by_id.get(str(candidate.get("candidate_id") or ""))
    if not previous:
        return candidate
    for key in ["review_status", "review_required", "reviewer", "review_note", "reviewed_at", "review_decision"]:
        if key in previous:
            candidate[key] = previous[key]
    if previous.get("promotion_status"):
        candidate["promotion_status"] = previous["promotion_status"]
    if previous.get("gate_status") == "reviewed":
        candidate["gate_status"] = "reviewed"
    return candidate


def _record_payload(item: object) -> dict[str, object]:
    if is_dataclass(item):
        return asdict(item)
    return dict(item) if isinstance(item, dict) else {}


def _record_list(item: object, key: str) -> list[str]:
    value = _record_payload(item).get(key, [])
    if not isinstance(value, list):
        return []
    return [str(entry) for entry in value if str(entry).strip()]


def _scope(task: Any) -> str:
    return str(getattr(task, "goal", "") or getattr(task, "current_step", "") or "").strip()[:500]


def _candidate_id(run_id: str, candidate_type: str, content: str) -> str:
    digest = hashlib.sha256(f"{candidate_type}:{content}".encode()).hexdigest()[:12]
    return f"memgate-{_safe_segment(run_id)}-{digest}"


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dedupe(values) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _safe_segment(value: str) -> str:
    return str(value or "run").replace("/", "_").replace("\\", "_").strip() or "run"


def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = ["build_memory_gate_candidates", "memory_gate_review_queue_records", "read_memory_gate_jsonl"]
