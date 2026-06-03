
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .pre_real_task_validation import PreRealTaskValidationReport


@dataclass(frozen=True)
class LLMActivationReadinessRequest:
    workspace: Path
    pre_real_report: PreRealTaskValidationReport | None = None


@dataclass(frozen=True)
class LLMActivationReadinessPhase:
    phase_id: str
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class LLMActivationReadinessReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    phases: tuple[LLMActivationReadinessPhase, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "phases": [phase.to_dict() for phase in self.phases],
        }


def readiness_phase(
    phase_id: str,
    ok: bool,
    evidence_refs: list[str],
    issues: list[str],
) -> LLMActivationReadinessPhase:
    return LLMActivationReadinessPhase(
        phase_id=phase_id,
        status="PASSED" if ok else "FAILED",
        evidence_refs=evidence_refs,
        issues=issues,
    )


def readiness_summary(phases: tuple[LLMActivationReadinessPhase, ...]) -> dict[str, int]:
    return {
        "failed": sum(phase.status == "FAILED" for phase in phases),
        "passed": sum(phase.status == "PASSED" for phase in phases),
        "total": len(phases),
    }


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "readiness_phase",
    "readiness_summary",
]
