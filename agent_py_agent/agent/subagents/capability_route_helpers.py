
from __future__ import annotations

"""helper dataclasses and functions for capability route logging and status updates.

这些函数和数据类从 capability service 拆出来，
让 services/capabilities/ 只保留能力路由的服务入口。
"""

import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ..file_io import append_jsonl
from .capability_scope import request_scope_snapshot
from .models import CapabilityRequest, SubAgentTask
from .reports import CapabilityRouteRecord
from .utils import _new_id

if TYPE_CHECKING:
    from ..capabilities import CapabilitySearchHit
    from .models import CapabilityGrant


@dataclass(frozen=True)
class RouteCapabilityGrantParams:
    """Params bundle for _route_capability_grant."""
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
    """Params bundle for _route_capability_apply."""
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
class RouteCapabilityGapParams:
    """Params bundle for _route_capability_gap."""

    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    gap: object


def _route_capability_gap(params: RouteCapabilityGapParams):
    """Build a GAP record when no hits found and apply=True."""
    now = time.time()
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=params.task.id,
        request_id=params.request.id,
        status="GAP",
        dry_run=False,
        query=params.query,
        candidate_count=len(params.hits),
        gap_id=params.gap.id,
        message="未找到足够可信的 skill/tool card，已记录 capability gap。",
        created_at=now,
    )


def _route_capability_grant(*, params: RouteCapabilityGrantParams) -> CapabilityRouteRecord:
    """Build a GRANTED record when hits found and apply=True."""
    task = params.task
    request = params.request
    query = params.query
    hits = params.hits
    selected_hits = params.selected_hits
    granted_skills = params.granted_skills
    granted_tools = params.granted_tools
    selected_cards = params.selected_cards
    reasons = params.reasons
    grant = params.grant
    now = time.time()
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=task.id,
        request_id=request.id,
        status="GRANTED",
        dry_run=False,
        query=query,
        candidate_count=len(hits),
        granted_skills=granted_skills,
        granted_tools=granted_tools,
        selected_cards=selected_cards,
        reasons=reasons,
        request_scope=request_scope_snapshot(request),
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
        message=_grant_route_message(grant, request),
        created_at=now,
    )


def _grant_route_message(grant, request: CapabilityRequest) -> str:
    if getattr(grant, "request_id", "") == request.id:
        return "已生成 capability grant。"
    return "已有 capability grant 覆盖该请求。"


def _mark_capability_request_status(manager, run_id: str, request_id: str, status: str) -> None:
    """更新 capability request 状态。"""
    task = manager.load(run_id)
    for request in task.capability_requests:
        if request.id == request_id:
            request.status = status
    task.updated_at = time.time()
    manager.save(task)


def _append_capability_route_log(manager, record: CapabilityRouteRecord) -> None:
    """写入 capability route 审计日志。"""
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
    manager._index_capability_route(record)
