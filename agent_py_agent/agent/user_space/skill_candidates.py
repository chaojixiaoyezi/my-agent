# LLM: Skill candidate storage captures learning suggestions without auto-installing skills.
# 模块用途: 记录 owner/task/agent 提出的 skill 候选，只进草稿账本，不自动提升成正式能力。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..io import append_jsonl


# LLM: SkillCandidate is intentionally generic so any task type can suggest reusable know-how.
# 类用途: 保存一条 skill 学习候选，包括摘要、来源任务和证据引用。
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


# LLM: SkillCandidateAppendResult keeps callers from depending on a raw path string.
# 类用途: 返回 skill 候选写入位置和记录内容，便于测试、doctor 和后续审核流程复用。
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
