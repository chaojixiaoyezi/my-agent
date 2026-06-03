from __future__ import annotations

"""Self-learning draft service for subagent managers.

This service only maintains reviewable learning candidates. It does not install
skills or write long-term memory directly.
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ...runtime_errors import runtime_error_report
from ..learning_similarity import _learning_similarity, _normalize_learning_text
from ..models import LearningCandidate, SubAgentTask
from ..utils import _new_id

_LEARNING_STATUSES = {"draft", "accepted", "rejected"}


@dataclass(frozen=True)
class UpdateLearningCandidateParams:
    """Params bundle for merging one lesson into a candidate."""

    candidate: LearningCandidate
    text: str
    normalized: str
    task: SubAgentTask
    now: float


@dataclass(frozen=True)
class LearningCandidateListReport:
    """Result of scanning learning drafts without hiding damaged ledger files."""

    candidates: list[LearningCandidate]
    load_errors: list[dict[str, object]]


def _candidate_confidence(occurrence_count: int) -> float:
    return round(min(0.95, 0.45 + max(0, occurrence_count - 1) * 0.12), 2)


def _normalize_candidate(payload: dict[str, object]) -> LearningCandidate:
    status = str(payload.get("status", "draft") or "draft").strip().lower()
    if status not in _LEARNING_STATUSES:
        status = "draft"
    lesson = str(payload.get("lesson", "") or "").strip()
    normalized_key = _normalize_learning_text(str(payload.get("normalized_key", "") or lesson))
    source_runs = [str(item) for item in payload.get("source_runs", []) or [] if str(item).strip()]
    evidence = [item for item in payload.get("evidence", []) or [] if isinstance(item, dict)]
    variants = [str(item) for item in payload.get("variants", []) or [] if str(item).strip()]
    return LearningCandidate(
        id=str(payload.get("id", "") or _new_id("learn")),
        lesson=lesson,
        normalized_key=normalized_key,
        status=status,
        confidence=_float_payload(payload, "confidence", 0.5),
        occurrence_count=max(1, _int_payload(payload, "occurrence_count", 1)),
        evidence_count=max(1, _int_payload(payload, "evidence_count", len(evidence) or 1)),
        source_runs=source_runs,
        evidence=evidence,
        variants=variants,
        created_at=_float_payload(payload, "created_at", 0.0),
        updated_at=_float_payload(payload, "updated_at", _float_payload(payload, "created_at", 0.0)),
    )


class SubAgentLearningService:
    """Stores and reviews self-learning draft candidates for one manager."""

    def __init__(self, manager: object) -> None:
        self.manager = manager

    def learning_drafts_dir(self) -> Path:
        root = getattr(self.manager, "workspace_root", None) or self.manager.workspace
        path = Path(root) / "data" / "learning_drafts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def learning_enabled(self) -> bool:
        return bool(getattr(self.manager, "enable_self_learning", False))

    def list_learning_candidates(self) -> list[LearningCandidate]:
        return self.list_learning_candidates_report().candidates

    def list_learning_candidates_report(self) -> LearningCandidateListReport:
        candidates: list[LearningCandidate] = []
        load_errors: list[dict[str, object]] = []
        for path in sorted(self.learning_drafts_dir().glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                load_errors.append(_learning_candidate_load_error(path, exc))
                continue
            if isinstance(payload, dict):
                candidates.append(_normalize_candidate(payload))
            else:
                load_errors.append(
                    _learning_candidate_load_error(
                        path,
                        ValueError("learning candidate payload is not a JSON object"),
                    )
                )
        return LearningCandidateListReport(
            candidates=sorted(candidates, key=_learning_candidate_sort_key),
            load_errors=load_errors,
        )

    def load_learning_candidate(self, candidate_id: str) -> LearningCandidate:
        path = self.learning_drafts_dir() / f"{candidate_id}.json"
        if not path.exists():
            raise FileNotFoundError(candidate_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise FileNotFoundError(candidate_id)
        return _normalize_candidate(payload)

    def save_learning_candidate(self, candidate: LearningCandidate) -> LearningCandidate:
        path = self.learning_drafts_dir() / f"{candidate.id}.json"
        candidate.evidence_count = len(candidate.evidence)
        candidate.source_runs = list(dict.fromkeys(candidate.source_runs))
        candidate.variants = list(dict.fromkeys(item for item in candidate.variants if item.strip()))
        path.write_text(json.dumps(asdict(candidate), ensure_ascii=False, indent=2), encoding="utf-8")
        return candidate

    def record_learning_candidates(
        self,
        task: SubAgentTask,
        lessons: list[str],
    ) -> list[LearningCandidate]:
        if not self.learning_enabled():
            return []
        now = time.time()
        created_or_updated: list[LearningCandidate] = []
        active_candidates = [item for item in self.list_learning_candidates() if item.status != "rejected"]
        for lesson in lessons:
            saved = self._record_one_learning_candidate(task, lesson, active_candidates, now)
            if saved is not None:
                active_candidates.append(saved)
                created_or_updated.append(saved)
        return created_or_updated

    def set_learning_candidate_status(self, candidate_id: str, status: str) -> LearningCandidate:
        normalized = str(status or "").strip().lower()
        if normalized not in _LEARNING_STATUSES:
            raise ValueError(f"unsupported learning candidate status: {status}")
        candidate = self.load_learning_candidate(candidate_id)
        candidate.status = normalized
        candidate.updated_at = time.time()
        return self.save_learning_candidate(candidate)

    def learning_stats(self) -> dict[str, object]:
        report = self.list_learning_candidates_report()
        candidates = report.candidates
        summary = {"draft": 0, "accepted": 0, "rejected": 0}
        total_evidence = 0
        high_confidence = 0
        for item in candidates:
            summary[item.status] = summary.get(item.status, 0) + 1
            total_evidence += item.evidence_count
            if item.confidence >= 0.75:
                high_confidence += 1
        return {
            "total": len(candidates),
            "draft": summary["draft"],
            "accepted": summary["accepted"],
            "rejected": summary["rejected"],
            "high_confidence": high_confidence,
            "total_evidence": total_evidence,
            "directory": str(self.learning_drafts_dir()),
            "load_errors": report.load_errors,
        }

    def _record_one_learning_candidate(
        self,
        task: SubAgentTask,
        lesson: str,
        active_candidates: list[LearningCandidate],
        now: float,
    ) -> LearningCandidate | None:
        text = str(lesson or "").strip()
        if not text:
            return None
        normalized = _normalize_learning_text(text)
        if not normalized:
            return None
        best, best_score = _find_best_candidate(normalized, active_candidates)
        if best is not None and best_score >= 0.45:
            return self._update_candidate(UpdateLearningCandidateParams(best, text, normalized, task, now))
        return self._create_candidate(text, normalized, task, now)

    def _update_candidate(self, params: UpdateLearningCandidateParams) -> LearningCandidate:
        candidate = params.candidate
        evidence_item = _candidate_evidence(params.task, params.now)
        candidate.occurrence_count += 1
        candidate.confidence = _candidate_confidence(candidate.occurrence_count)
        candidate.updated_at = params.now
        candidate.source_runs.append(params.task.id)
        if evidence_item not in candidate.evidence:
            candidate.evidence.append(evidence_item)
        if params.text not in candidate.variants:
            candidate.variants.append(params.text)
        if len(params.text) > len(candidate.lesson):
            candidate.lesson = params.text
            candidate.normalized_key = params.normalized
        return self.save_learning_candidate(candidate)

    def _create_candidate(self, text: str, normalized: str, task: SubAgentTask, now: float) -> LearningCandidate:
        candidate = LearningCandidate(
            id=_new_id("learn"),
            lesson=text,
            normalized_key=normalized,
            status="draft",
            confidence=_candidate_confidence(1),
            occurrence_count=1,
            evidence_count=1,
            source_runs=[task.id],
            evidence=[_candidate_evidence(task, now)],
            variants=[text],
            created_at=now,
            updated_at=now,
        )
        return self.save_learning_candidate(candidate)


def _find_best_candidate(
    normalized: str,
    active_candidates: list[LearningCandidate],
) -> tuple[LearningCandidate | None, float]:
    best: LearningCandidate | None = None
    best_score = 0.0
    for candidate in active_candidates:
        score = _learning_similarity(normalized, candidate.normalized_key)
        if score > best_score:
            best = candidate
            best_score = score
    return best, best_score


def _learning_candidate_sort_key(item: LearningCandidate) -> tuple[int, float, float]:
    return (
        {"draft": 0, "accepted": 1, "rejected": 2}.get(item.status, 9),
        -item.confidence,
        -item.updated_at,
    )


def _candidate_evidence(task: SubAgentTask, now: float) -> dict[str, object]:
    return {
        "run_id": task.id,
        "output_json": task.output_json,
        "task_dir": task.task_dir,
        "recorded_at": now,
    }


def _learning_candidate_load_error(path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="subagent_learning.candidates.load")
    return {
        "path": str(path),
        "error": report,
    }


def _float_payload(payload: dict[str, object], key: str, default: float) -> float:
    try:
        return float(payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _int_payload(payload: dict[str, object], key: str, default: int) -> int:
    try:
        return int(payload.get(key, default) or default)
    except (TypeError, ValueError):
        return default


__all__ = [
    "LearningCandidateListReport",
    "SubAgentLearningService",
    "UpdateLearningCandidateParams",
    "_candidate_confidence",
    "_find_best_candidate",
    "_normalize_candidate",
]
