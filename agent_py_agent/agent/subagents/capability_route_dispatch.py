
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .capability_route_helpers import (
    RouteCapabilityGrantParams,
    _mark_capability_request_status,
    _route_capability_grant,
)
from .capability_route_service import (
    WouldGrantRecordParams,
    build_would_gap_record,
    build_would_grant_record,
    record_capability_route_gap,
)
from .models import CapabilityRequest, SubAgentTask
from .reports import CapabilityRouteRecord

if TYPE_CHECKING:
    from ..capabilities import CapabilitySearchHit


@dataclass(frozen=True)
class CapabilityNoHitsParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    apply: bool


@dataclass(frozen=True)
class WouldCapabilityGrantParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    granted_skills: list[str]
    granted_tools: list[str]
    selected_cards: list[dict[str, str]]
    reasons: list[str]


@dataclass(frozen=True)
class ExistingCapabilityGrantParams:
    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    apply: bool
    grant: object


def route_capability_no_hits(manager, params: CapabilityNoHitsParams):
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
    manager._append_task_work_log(
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
