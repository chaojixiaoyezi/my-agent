
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MainAgentFoundationRequest:
    workspace: Path
    include_real_model: bool = False


@dataclass(frozen=True)
class MainAgentFoundationCaseResult:
    case_id: str
    title: str
    status: str
    summary: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "summary": self.summary,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class MainAgentFoundationReport:
    ok: bool
    summary: dict[str, int]
    results: list[MainAgentFoundationCaseResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "results": [item.to_dict() for item in self.results],
        }


__all__ = [
    "MainAgentFoundationCaseResult",
    "MainAgentFoundationReport",
    "MainAgentFoundationRequest",
]
