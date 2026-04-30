from __future__ import annotations

"""Local dispatch engine for log-analysis analyst work."""

import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..agents.contracts import normalize_evidence_refs, review_analyst_report
from ..agents.summaries import render_case_summary, summarize_case
from .budgets import DispatchBudget
from .queue import DispatchRequest, InvestigationQueue


@dataclass
class DispatchResult:
    request: DispatchRequest
    dispatched: bool
    reason: str
    agent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["request"] = self.request.to_dict()
        return payload


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_id(case: Any, summary: dict[str, Any]) -> str:
    return str(
        _get(case, "case_id")
        or _get(case, "id")
        or summary.get("case", {}).get("case_id")
        or "unknown-case"
    )


def _priority(case: Any, summary: dict[str, Any]) -> str:
    return str(
        _get(case, "priority")
        or _get(case, "severity")
        or summary.get("case", {}).get("priority")
        or summary.get("case", {}).get("severity")
        or ""
    )


def _evidence_refs_from_summary(case: Any, summary: dict[str, Any]) -> list[str]:
    refs = normalize_evidence_refs(_get(case, "evidence_refs") or _get(case, "evidence"))
    if refs:
        return refs
    return normalize_evidence_refs(summary.get("evidence"))


class DispatchEngine:
    """Create dispatch requests without making the parent session do analysis."""

    def __init__(
        self,
        *,
        queue: InvestigationQueue | None = None,
        budget: DispatchBudget | Mapping[str, Any] | None = None,
    ):
        self.queue = queue or InvestigationQueue()
        self.budget = budget if isinstance(budget, DispatchBudget) else DispatchBudget.from_mapping(budget)

    def submit_case(self, case: Any) -> DispatchResult:
        """Add a case to the queue and dispatch only when budget allows."""

        summary_obj = summarize_case(case)
        summary = summary_obj.to_dict()
        case_id = _case_id(case, summary)
        priority = _priority(case, summary)
        evidence_refs = _evidence_refs_from_summary(case, summary)
        decision = self.budget.can_dispatch_analyst(
            priority=priority,
            active_analyst_agents=len(self.queue.active(role="analyst")),
            analyst_dispatches_last_hour=len(
                self.queue.dispatched_since(time.time() - 3600, role="analyst")
            ),
        )

        request = self.queue.add_pending(
            case_id=case_id,
            priority=priority,
            reason=decision.reason if not decision.allowed else "pending dispatch",
            evidence_refs=evidence_refs,
            case_summary=summary.get("case", {}),
            route_summary=summary.get("route", {}),
        )

        if not decision.allowed:
            return DispatchResult(request=request, dispatched=False, reason=decision.reason)

        agent_id = f"analyst-{request.request_id}"
        self.queue.mark_dispatched(request.request_id, agent_id=agent_id)
        return DispatchResult(request=request, dispatched=True, reason="dispatched", agent_id=agent_id)

    def build_analyst_input(self, request: DispatchRequest) -> dict[str, Any]:
        """Return the small object sent to an analyst subagent."""

        return {
            "case_id": request.case_id,
            "case_summary": render_case_summary(
                {
                    "case": request.case_summary,
                    "evidence": request.evidence_refs,
                    "route": request.route_summary,
                }
            ),
            "evidence_refs": list(request.evidence_refs),
            "route_summary": dict(request.route_summary),
            "available_tools": [
                "traffic_query",
                "traffic_topn",
                "traffic_sample",
                "traffic_timeseries",
                "traffic_related",
                "evidence_read",
            ],
            "budget": {
                "max_case_rounds": self.budget.max_case_rounds,
                "timeout_seconds": self.budget.analyst_agent_timeout_seconds,
            },
        }

    def review_report(
        self,
        report: Any,
        *,
        known_evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        return review_analyst_report(report, known_evidence_refs=known_evidence_refs).to_dict()
