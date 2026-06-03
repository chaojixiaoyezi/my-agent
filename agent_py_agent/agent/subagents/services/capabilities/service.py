from __future__ import annotations

"""Capability request routing service for subagent tasks."""

from typing import TYPE_CHECKING

from ....capabilities import CapabilityRouter
from ....capability_config import CapabilityConfig
from ...capability_route_dispatch import (
    CapabilityNoHitsParams,
    ExistingCapabilityGrantParams,
    WouldCapabilityGrantParams,
    route_capability_no_hits,
    route_existing_capability_grant,
    route_would_capability_grant,
)
from ...capability_route_helpers import (
    RouteCapabilityApplyParams,
    RouteCapabilityGrantParams,
    _mark_capability_request_status,
    _route_capability_grant,
)
from ...capability_route_service import (
    build_capability_route_report,
    extract_selected_hits_data,
    write_capability_route_report_files,
)
from ...capability_scope import existing_delete_trash_grant, scoped_grant_params
from ...models import CapabilityRequest, SubAgentCapabilityRouteOptions, SubAgentTask
from ...policies import _capability_request_query, _select_capability_hits
from ...reports import CapabilityRouteRecord, CapabilityRouteReport

if TYPE_CHECKING:
    from ....capabilities import CapabilitySearchHit


def _iter_open_capability_requests(tasks):
    return (
        (task, request)
        for task in tasks
        for request in task.capability_requests
        if request.status == "OPEN"
    )


class SubAgentCapabilityService:
    """Route OPEN capability requests to available skill/tool cards."""

    def __init__(self, manager):
        self.manager = manager

    def route_capability_requests(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        params: SubAgentCapabilityRouteOptions | None = None,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        options = _capability_route_options(params, apply=apply, run_ids=run_ids, limit=limit)
        cfg = config or CapabilityConfig()
        records: list[CapabilityRouteRecord] = []
        selected_runs = self.manager._select_runs(options.run_ids)
        for task, request in _iter_open_capability_requests(selected_runs):
            query = _capability_request_query(task, request)
            hits = router.search(query, limit=cfg.capability_candidate_limit)
            selected_hits = _select_capability_hits(hits, cfg)
            records.append(
                self._route_capability_request(
                    task,
                    request,
                    query=query,
                    hits=hits,
                    selected_hits=selected_hits,
                    apply=options.apply,
                )
            )
            if options.limit > 0 and len(records) >= options.limit:
                break
        return build_capability_route_report(records, apply=options.apply)

    def write_capability_route_report(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        params: SubAgentCapabilityRouteOptions | None = None,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        options = _capability_route_options(params, apply=apply, run_ids=run_ids, limit=limit)
        report = self.route_capability_requests(router, config, params=options)
        write_capability_route_report_files(self.manager, report, apply=options.apply)
        return report

    def _route_capability_apply(
        self,
        *,
        params: RouteCapabilityApplyParams,
    ) -> CapabilityRouteRecord:
        grant = self.manager.record_capability_grant(
            params.task.id,
            scoped_grant_params(
                params.request,
                routed_skills=params.granted_skills,
                routed_tools=params.granted_tools,
                selected_cards=params.selected_cards,
                hit_count=len(params.selected_hits),
            ),
        )
        _mark_capability_request_status(self.manager, params.task.id, params.request.id, "GRANTED")
        routed_task = self.manager.load(params.task.id)
        self.manager._append_task_work_log(
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

    def _route_capability_request(
        self,
        task: SubAgentTask,
        request: CapabilityRequest,
        *,
        query: str,
        hits: list[CapabilitySearchHit],
        selected_hits: list[CapabilitySearchHit],
        apply: bool,
    ) -> CapabilityRouteRecord:
        selected_hits = _hits_matching_requested_scope(request, selected_hits)
        selected_cards, granted_skills, granted_tools, reasons = extract_selected_hits_data(selected_hits)
        if existing_grant := existing_delete_trash_grant(task, request):
            return route_existing_capability_grant(
                self.manager,
                ExistingCapabilityGrantParams(task, request, query, hits, apply, existing_grant),
            )
        if not selected_hits:
            return route_capability_no_hits(
                self.manager,
                CapabilityNoHitsParams(task, request, query, hits, apply),
            )
        if not apply:
            return route_would_capability_grant(
                WouldCapabilityGrantParams(
                    task, request, query, hits, granted_skills, granted_tools, selected_cards, reasons
                )
            )
        return self._route_capability_apply(
            params=RouteCapabilityApplyParams(
                task=task,
                request=request,
                query=query,
                hits=hits,
                selected_hits=selected_hits,
                granted_skills=granted_skills,
                granted_tools=granted_tools,
                selected_cards=selected_cards,
                reasons=reasons,
            )
        )


def _hits_matching_requested_scope(
    request: CapabilityRequest,
    hits: list[CapabilitySearchHit],
) -> list[CapabilitySearchHit]:
    requested_tools = set(request.requested_tools or [])
    requested_skills = set(request.requested_skills or [])
    requested_mcp = set(request.requested_mcp_tools or [])
    if requested_mcp:
        return [hit for hit in hits if hit.card.kind == "mcp" and hit.card.name in requested_mcp]
    if requested_tools:
        return [hit for hit in hits if hit.card.kind == "tool" and hit.card.name in requested_tools]
    if requested_skills:
        return [hit for hit in hits if hit.card.kind == "skill" and hit.card.name in requested_skills]
    return hits


def _capability_route_options(
    params: SubAgentCapabilityRouteOptions | None,
    *,
    apply: bool,
    run_ids: list[str] | None,
    limit: int,
) -> SubAgentCapabilityRouteOptions:
    if params is not None:
        if not isinstance(params, SubAgentCapabilityRouteOptions):
            raise TypeError("capability routing requires params: SubAgentCapabilityRouteOptions")
        return params
    return SubAgentCapabilityRouteOptions(apply=apply, run_ids=run_ids, limit=limit)
