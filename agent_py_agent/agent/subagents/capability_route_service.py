
from __future__ import annotations

"""Helpers for capability request routing records and report persistence."""

import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .capability_route_helpers import (
    _append_capability_route_log,
    _mark_capability_request_status,
)
from .capability_scope import (
    escalation_chain,
    gap_attempted_tools,
    request_scope_snapshot,
    scoped_constraints,
)
from .policies import _route_card_payload
from .rendering import render_capability_route_markdown
from .reports import CapabilityRouteRecord, CapabilityRouteReport
from .services.indexing.params import IndexReportParams
from .services.lifecycle import RecordCapabilityGapParams
from .utils import _merge_list, _new_id

if TYPE_CHECKING:
    from agent_py_agent.agent.capability import CapabilitySearchHit

    from .models import CapabilityRequest, SubAgentTask


@dataclass
class WouldGrantRecordParams:

    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    granted_skills: list[str]
    granted_tools: list[str]
    selected_cards: list[dict[str, str]]
    reasons: list[str]
    created_at: float | None = None


def extract_selected_hits_data(
    selected_hits: list[CapabilitySearchHit],
) -> tuple[list[dict[str, str]], list[str], list[str], list[str]]:
    """Extract cards, skills, tools, and reasons from selected hits."""
    selected_cards = [_route_card_payload(hit) for hit in selected_hits]
    granted_skills = [hit.card.name for hit in selected_hits if hit.card.kind == "skill"]
    granted_tools = [hit.card.name for hit in selected_hits if hit.card.kind == "tool"]
    reasons = _merge_list([], [reason for hit in selected_hits for reason in hit.reasons])
    return selected_cards, granted_skills, granted_tools, reasons


def build_would_gap_record(
    task: SubAgentTask,
    request: CapabilityRequest,
    *,
    query: str,
    hits: list[CapabilitySearchHit],
    created_at: float | None = None,
) -> CapabilityRouteRecord:
    """Build a dry-run WOULD_GAP route record."""
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=task.id,
        request_id=request.id,
        status="WOULD_GAP",
        dry_run=True,
        query=query,
        candidate_count=len(hits),
        request_scope=request_scope_snapshot(request),
        message="未找到足够可信的 skill/tool card；apply 时会记录 capability gap。",
        created_at=created_at or time.time(),
    )


def record_capability_route_gap(
    manager,
    task: SubAgentTask,
    request: CapabilityRequest,
    *,
    query: str,
    hits: list[CapabilitySearchHit],
    created_at: float | None = None,
) -> CapabilityRouteRecord:
    """Record a capability gap and build the applied GAP route record."""
    gap = manager.record_capability_gap(
        task.id,
        RecordCapabilityGapParams(
            missing_capability=request.needed_capability,
            why_failed="CapabilityRouter 没有找到匹配的 skill/tool card。",
            gap_type=request.capability_type,
            attempted_tools=gap_attempted_tools(request),
            needed_outputs=[request.expected_output] if request.expected_output else [],
            requested_scope=request_scope_snapshot(request),
            constraints=scoped_constraints(request),
            escalation_chain=escalation_chain(task, request),
            next_record_refs=[f"capability_request:{request.id}"],
        ),
    )
    _mark_capability_request_status(manager, task.id, request.id, "GAP")
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=task.id,
        request_id=request.id,
        status="GAP",
        dry_run=False,
        query=query,
        candidate_count=len(hits),
        request_scope=request_scope_snapshot(request),
        grant_scope={
            "grant_type": request.capability_type,
            "constraints": scoped_constraints(request),
            "requested_scope": request_scope_snapshot(request),
        },
        gap_id=gap.id,
        message="未找到足够可信的 skill/tool card，已记录 capability gap。",
        created_at=created_at or time.time(),
    )


def build_would_grant_record(params: WouldGrantRecordParams) -> CapabilityRouteRecord:
    """Build a dry-run WOULD_GRANT route record."""
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=params.task.id,
        request_id=params.request.id,
        status="WOULD_GRANT",
        dry_run=True,
        query=params.query,
        candidate_count=len(params.hits),
        granted_skills=params.granted_skills,
        granted_tools=params.granted_tools,
        selected_cards=params.selected_cards,
        reasons=params.reasons,
        request_scope=request_scope_snapshot(params.request),
        grant_scope={
            "grant_type": params.request.capability_type,
            "constraints": scoped_constraints(params.request),
            "requested_scope": request_scope_snapshot(params.request),
        },
        message="找到候选能力；apply 时会生成 capability grant。",
        created_at=params.created_at or time.time(),
    )


def build_capability_route_report(
    records: list[CapabilityRouteRecord],
    *,
    apply: bool,
) -> CapabilityRouteReport:
    """Build the aggregate capability route report."""
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.status] = summary.get(record.status, 0) + 1
        summary["dry_run" if record.dry_run else "applied"] = summary.get(
            "dry_run" if record.dry_run else "applied",
            0,
        ) + 1
    return CapabilityRouteReport(
        generated_at=time.time(),
        dry_run=not apply,
        summary=summary,
        records=records,
    )


def write_capability_route_report_files(
    manager,
    report: CapabilityRouteReport,
    *,
    apply: bool,
) -> None:
    """Persist route report artifacts and applied-route audit records."""
    (manager.workspace / "subagent_capability_route_report.json").write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (manager.workspace / "SUBAGENT_CAPABILITY_ROUTE.md").write_text(
        render_capability_route_markdown(report),
        encoding="utf-8",
    )
    manager._index_report(
        IndexReportParams(
            "subagent_capability_route_report",
            "latest",
            "Subagent capability route report",
            report,
            "subagent_capability_route_report_written",
        ),
    )
    if apply:
        for record in report.records:
            _append_capability_route_log(manager, record)
