# LLM: This service routes only OPEN typed requests. Objective owner-scope violations become GAP
# before semantic card selection and can never create a grant or continuation wake.
# 模块用途: 为子代理能力申请匹配工具/技能，并把无法授权的范围记录为结构化缺口。
from __future__ import annotations

from typing import TYPE_CHECKING

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ..capability_route_service import (
    CapabilityNoHitsParams,
    CapabilityOwnerScopeGapParams,
    ExistingCapabilityGrantParams,
    RouteCapabilityApplyParams,
    WouldCapabilityGrantParams,
    build_capability_route_report,
    extract_selected_hits_data,
    route_capability_apply,
    route_capability_no_hits,
    route_capability_owner_scope_gap,
    route_existing_capability_grant,
    route_would_capability_grant,
    write_capability_route_report_files,
)
from ..capability_scope import (
    effective_request_path_scope,
    existing_delete_trash_grant,
    partition_capability_paths_by_owner,
)
from ..model_capabilities import capability_request_counts_as_open
from ..models import CapabilityRequest, SubAgentCapabilityRouteOptions, SubAgentTask
from ..policies import _capability_request_query, _select_capability_hits
from ..reports import CapabilityRouteRecord, CapabilityRouteReport

if TYPE_CHECKING:
    from agent_py_agent.agent.capability import CapabilitySearchHit


def _iter_open_capability_requests(tasks):
    return (
        (task, request)
        for task in tasks
        for request in task.capability_requests
        if capability_request_counts_as_open(getattr(request, "status", "OPEN"))
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
        selected_runs = self.manager.indexing.select_runs(options.run_ids)
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

    # LLM: Owner scope is checked before existing-grant reuse or semantic hit application. Keep
    # this ordering so no broad historical grant can bypass the current tenant boundary.
    # 函数用途: 路由一条能力申请，越过用户目录时直接记缺口，否则再匹配或复用能力卡。
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
        scope_decision = partition_capability_paths_by_owner(
            self.manager,
            task,
            effective_request_path_scope(request),
        )
        if scope_decision.rejected_paths:
            return route_capability_owner_scope_gap(
                self.manager,
                CapabilityOwnerScopeGapParams(
                    task=task,
                    request=request,
                    query=query,
                    hits=hits,
                    decision=scope_decision,
                    apply=apply,
                ),
            )
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
        return route_capability_apply(
            self.manager,
            RouteCapabilityApplyParams(
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
        return [
            hit
            for hit in hits
            if hit.card.kind == "skill"
            and (
                hit.card.name in requested_skills
                or str(hit.card.metadata.get("stable_id") or "") in requested_skills
            )
        ]
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
