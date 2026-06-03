
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl


@dataclass(frozen=True)
class SkillCandidate:
    title: str
    summary: str
    source_task_id: str = ""
    source_run_id: str = ""
    source_agent_id: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_record(self) -> dict[str, object]:
        payload = asdict(self)
        if not payload["created_at"]:
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
        payload["promotion_status"] = "candidate"
        return payload


@dataclass(frozen=True)
class SkillCandidateAppendResult:
    path: Path
    record: dict[str, object]


def append_owner_skill_candidate(home_paths: Any, candidate: SkillCandidate) -> SkillCandidateAppendResult:
    draft_dir = Path(home_paths.owner_home_dir) / "skills" / ".drafts"
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / "skill_candidates.jsonl"
    record = candidate.to_record()
    append_jsonl(path, record, sort_keys=True)
    return SkillCandidateAppendResult(path=path, record=record)


__all__ = ["SkillCandidate", "SkillCandidateAppendResult", "append_owner_skill_candidate"]
