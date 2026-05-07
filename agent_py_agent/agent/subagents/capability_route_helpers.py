from __future__ import annotations

"""LLM: helper dataclasses and functions for capability route logging and status updates.

给人看的解释：
这些函数和数据类从 manager_capabilities.py 拆出来，
让 manager_capabilities.py 只保留 SubAgentCapabilityMixin 类本身。
"""

import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ..file_io import append_jsonl
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
        grant_id=grant.id,
        message="已生成 capability grant。",
        created_at=now,
    )


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
