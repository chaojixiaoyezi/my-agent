
from __future__ import annotations

"""Helpers for capability request routing records and report persistence."""

import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ..io import append_jsonl
from .capability_scope import (
    escalation_chain,
    existing_delete_trash_grant,
    gap_attempted_tools,
    request_scope_snapshot,
    scoped_constraints,
    scoped_grant_params,
)
from .policies import _route_card_payload
from .rendering import render_capability_route_markdown
from .reports import CapabilityRouteRecord, CapabilityRouteReport
from .services.indexing.params import IndexReportParams
from .services.lifecycle import RecordCapabilityGapParams
from .utils import _merge_list, _new_id

if TYPE_CHECKING:
    from agent_py_agent.agent.capability import CapabilitySearchHit

    from .models import CapabilityGrant, CapabilityRequest, SubAgentTask


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


@dataclass(frozen=True)
class RouteCapabilityGrantParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    selected_hits: list[CapabilitySearchHit]
    granted_skills: list[str]
    granted_tools: list[str]
    selected_cards: list[dict[str, str]]
    reasons: list[str]
    grant: CapabilityGrant


@dataclass(frozen=True)
class RouteCapabilityApplyParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    selected_hits: list[CapabilitySearchHit]
    granted_skills: list[str]
    granted_tools: list[str]
    selected_cards: list[dict[str, str]]
    reasons: list[str]


@dataclass(frozen=True)
class CapabilityNoHitsParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    apply: bool


@dataclass(frozen=True)
class WouldCapabilityGrantParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    granted_skills: list[str]
    granted_tools: list[str]
    selected_cards: list[dict[str, str]]
    reasons: list[str]


@dataclass(frozen=True)
class ExistingCapabilityGrantParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list[CapabilitySearchHit]
    apply: bool
    grant: object


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
    gap = manager.lifecycle.record_capability_gap(
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


def route_capability_no_hits(manager, params: CapabilityNoHitsParams) -> CapabilityRouteRecord:
    now = time.time()
    if not params.apply:
        return build_would_gap_record(params.task, params.request, query=params.query, hits=params.hits, created_at=now)
    return record_capability_route_gap(
        manager,
        params.task,
        params.request,
        query=params.query,
        hits=params.hits,
        created_at=now,
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


def route_would_capability_grant(params: WouldCapabilityGrantParams) -> CapabilityRouteRecord:
    return build_would_grant_record(
        WouldGrantRecordParams(
            task=params.task,
            request=params.request,
            query=params.query,
            hits=params.hits,
            granted_skills=params.granted_skills,
            granted_tools=params.granted_tools,
            selected_cards=params.selected_cards,
            reasons=params.reasons,
            created_at=time.time(),
        )
    )


def route_existing_capability_grant(manager, params: ExistingCapabilityGrantParams) -> CapabilityRouteRecord:
    reasons = ["已有 controlled_exec grant；删除命令应通过 task_trash 处理，不进入 shell 白名单。"]
    granted_skills = list(getattr(params.grant, "skills", []) or [])
    granted_tools = list(getattr(params.grant, "tools", []) or [])
    selected_cards = list(getattr(params.grant, "capability_cards", []) or [])
    if not params.apply:
        return route_would_capability_grant(
            WouldCapabilityGrantParams(
                task=params.task,
                request=params.request,
                query=params.query,
                hits=params.hits,
                granted_skills=granted_skills,
                granted_tools=granted_tools,
                selected_cards=selected_cards,
                reasons=reasons,
            )
        )
    _mark_capability_request_status(manager, params.task.id, params.request.id, "GRANTED")
    routed_task = manager.load(params.task.id)
    manager.actions._append_task_work_log(
        routed_task,
        f"capability_route: request {params.request.id} 由已有 grant {params.grant.id} 覆盖，删除命令走 task_trash。",
    )
    return _route_capability_grant(
        params=RouteCapabilityGrantParams(
            task=params.task,
            request=params.request,
            query=params.query,
            hits=params.hits,
            selected_hits=[],
            granted_skills=granted_skills,
            granted_tools=granted_tools,
            selected_cards=selected_cards,
            reasons=reasons,
            grant=params.grant,
        )
    )


def route_capability_apply(manager, params: RouteCapabilityApplyParams) -> CapabilityRouteRecord:
    grant = manager.lifecycle.record_capability_grant(
        params.task.id,
        scoped_grant_params(
            params.request,
            routed_skills=params.granted_skills,
            routed_tools=params.granted_tools,
            selected_cards=params.selected_cards,
            hit_count=len(params.selected_hits),
        ),
    )
    _mark_capability_request_status(manager, params.task.id, params.request.id, "GRANTED")
    routed_task = manager.load(params.task.id)
    manager.actions._append_task_work_log(
        routed_task,
        f"capability_route: request {params.request.id} 已生成 grant {grant.id}，"
        f"skills={','.join(params.granted_skills) or 'none'} "
        f"tools={','.join(params.granted_tools) or 'none'}。",
    )
    return _route_capability_grant(
        params=RouteCapabilityGrantParams(
            task=params.task,
            request=params.request,
            query=params.query,
            hits=params.hits,
            selected_hits=params.selected_hits,
            granted_skills=params.granted_skills,
            granted_tools=params.granted_tools,
            selected_cards=params.selected_cards,
            reasons=params.reasons,
            grant=grant,
        )
    )


def _route_capability_grant(*, params: RouteCapabilityGrantParams) -> CapabilityRouteRecord:
    grant = params.grant
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=params.task.id,
        request_id=params.request.id,
        status="GRANTED",
        dry_run=False,
        query=params.query,
        candidate_count=len(params.hits),
        granted_skills=params.granted_skills,
        granted_tools=params.granted_tools,
        selected_cards=params.selected_cards,
        reasons=params.reasons,
        request_scope=request_scope_snapshot(params.request),
        grant_scope={
            "grant_id": grant.id,
            "grant_type": grant.grant_type,
            "tools": list(grant.tools),
            "skills": list(grant.skills),
            "mcp_tools": list(grant.mcp_tools),
            "command_allowlist": list(grant.command_allowlist),
            "path_scope": list(grant.path_scope),
            "network_scope": list(grant.network_scope),
            "output_budget": dict(grant.output_budget),
            "constraints": dict(grant.constraints),
        },
        grant_id=grant.id,
        message=_grant_route_message(grant, params.request),
        created_at=time.time(),
    )


def _grant_route_message(grant: CapabilityGrant, request: CapabilityRequest) -> str:
    if getattr(grant, "request_id", "") == request.id:
        return "已生成 capability grant。"
    return "已有 capability grant 覆盖该请求。"


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
    manager.indexing.index_report(
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


def _mark_capability_request_status(manager, run_id: str, request_id: str, status: str) -> None:
    task = manager.load(run_id)
    for request in task.capability_requests:
        if request.id == request_id:
            request.status = status
    task.updated_at = time.time()
    manager.save(task)


def _append_capability_route_log(manager, record: CapabilityRouteRecord) -> None:
    jsonl = manager.workspace / "subagent_capability_route_log.jsonl"
    append_jsonl(jsonl, asdict(record))
    markdown = manager.workspace / "CAPABILITY_ROUTE_LOG.md"
    if not markdown.exists():
        markdown.write_text("# CAPABILITY ROUTE LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        handle.write(
            f"- [{record.status}] {record.id} run={record.run_id} request={record.request_id} "
            f"skills={','.join(record.granted_skills) or 'none'} "
            f"tools={','.join(record.granted_tools) or 'none'} message={record.message}\n"
        )
    manager.indexing.index_capability_route(record)
