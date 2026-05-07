from __future__ import annotations

"""Small case scheduler for detector output."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import CaseRecord, Finding
from .case_store import CaseStore


@dataclass
class ScheduleResult:
    recorded_findings: list[str] = field(default_factory=list)
    low_confidence_findings: list[str] = field(default_factory=list)
    cases: list[CaseRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_findings": list(self.recorded_findings),
            "low_confidence_findings": list(self.low_confidence_findings),
            "case_ids": [case.case_id for case in self.cases],
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True)
class ScheduleFindingsOptions:
    root: str | Path | None = None
    store: CaseStore | None = None
    min_case_confidence: float = 0.6
    merge_window_minutes: int = 15
    search_store: Any | None = None


class CaseScheduler:
    def __init__(self, store: CaseStore, *, min_case_confidence: float | None = None) -> None:
        self.store = store
        self.min_case_confidence = min_case_confidence

    def schedule(self, findings: Sequence[Finding | Mapping[str, Any]]) -> ScheduleResult:
        result = ScheduleResult()
        seen_cases: set[str] = set()
        original_threshold = self.store.min_case_confidence
        if self.min_case_confidence is not None:
            self.store.min_case_confidence = self.min_case_confidence
        try:
            for item in findings:
                _record_scheduled_finding(self.store, result, seen_cases, item)
        finally:
            self.store.min_case_confidence = original_threshold
        return result


def _record_scheduled_finding(
    store: CaseStore,
    result: ScheduleResult,
    seen_cases: set[str],
    item: Finding | Mapping[str, Any],
) -> None:
    finding = item if isinstance(item, Finding) else Finding.from_dict(item)
    result.recorded_findings.append(finding.finding_id)
    case = store.record_finding(finding)
    if case is None:
        result.low_confidence_findings.append(finding.finding_id)
        return
    if case.case_id not in seen_cases:
        seen_cases.add(case.case_id)
        result.cases.append(case)


def schedule_findings(
    findings: Sequence[Finding | Mapping[str, Any]],
    *,
    options: ScheduleFindingsOptions | None = None,
    root: str | Path | None = None,
    store: CaseStore | None = None,
    min_case_confidence: float = 0.6,
    merge_window_minutes: int = 15,
    search_store: Any | None = None,
) -> ScheduleResult:
    schedule_options = options or ScheduleFindingsOptions(
        root=root,
        store=store,
        min_case_confidence=float(min_case_confidence),
        merge_window_minutes=int(merge_window_minutes),
        search_store=search_store,
    )
    case_store = schedule_options.store or CaseStore(
        schedule_options.root or Path("data") / "log_analysis",
        min_case_confidence=schedule_options.min_case_confidence,
        merge_window_minutes=schedule_options.merge_window_minutes,
        search_store=schedule_options.search_store,
    )
    return CaseScheduler(case_store).schedule(findings)


def findings_to_cases(
    findings: Sequence[Finding | Mapping[str, Any]],
    *,
    options: ScheduleFindingsOptions | None = None,
    root: str | Path | None = None,
    store: CaseStore | None = None,
    min_case_confidence: float = 0.6,
    merge_window_minutes: int = 15,
    search_store: Any | None = None,
) -> list[CaseRecord]:
    return schedule_findings(
        findings,
        options=options,
        root=root,
        store=store,
        min_case_confidence=min_case_confidence,
        merge_window_minutes=merge_window_minutes,
        search_store=search_store,
    ).cases


__all__ = ["CaseScheduler", "ScheduleFindingsOptions", "ScheduleResult", "findings_to_cases", "schedule_findings"]
