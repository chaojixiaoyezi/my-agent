from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from .capability_route_helpers import (
    RouteCapabilityApplyParams,
    RouteCapabilityGrantParams,
    _mark_capability_request_status,
    _route_capability_grant,
)
from .capability_route_service import (
    WouldGrantRecordParams,
    build_capability_route_report,
    build_would_gap_record,
    build_would_grant_record,
    extract_selected_hits_data,
    record_capability_route_gap,
    write_capability_route_report_files,
)
from .models import CapabilityRequest, SubAgentCapabilityRouteOptions, SubAgentTask
from .policies import _capability_request_query, _select_capability_hits
from .reports import CapabilityRouteRecord, CapabilityRouteReport
from .services.lifecycle import RecordCapabilityGrantParams

if TYPE_CHECKING:
    from ..capabilities import CapabilitySearchHit


def _iter_open_capability_requests(tasks):
    return (
        (task, request)
        for task in tasks
        for request in task.capability_requests
        if request.status == "OPEN"
    )


@dataclass(frozen=True)
class CapabilityNoHitsParams:
    """LLM: bundle no-hit route state for dry-run/apply decisions."""

    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    apply: bool


class SubAgentCapabilityMixin:
    def _route_capability_no_hits(self, params: CapabilityNoHitsParams):
        now = time.time()
        if not params.apply:
            return build_would_gap_record(params.task, params.request, query=params.query, hits=params.hits, created_at=now)
        return record_capability_route_gap(
            self,
            params.task,
            params.request,
            query=params.query,
            hits=params.hits,
            created_at=now,
        )

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
        """把 OPEN capability request 路由到 skill/tool card。
        默认 dry-run，只展示会下发哪些能力。
        `apply=True` 时才会真正生成 capability grant 或 capability gap。
        """
        options = _capability_route_options(params, apply=apply, run_ids=run_ids, limit=limit)
        cfg = config or CapabilityConfig()
        records: list[CapabilityRouteRecord] = []
        selected_runs = self._select_runs(options.run_ids)
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
        report = self.route_capability_requests(
            router,
            config,
            params=options,
        )
        write_capability_route_report_files(self, report, apply=options.apply)
        return report

    def _extract_selected_hits_data(
        self,
        selected_hits: list[CapabilitySearchHit],
    ) -> tuple[list[dict[str, str]], list[str], list[str], list[str]]:
        return extract_selected_hits_data(selected_hits)
    def _route_capability_apply(
        self,
        *,
        params: RouteCapabilityApplyParams,
    ) -> CapabilityRouteRecord:
        grant = self.record_capability_grant(
            params.task.id,
            RecordCapabilityGrantParams(
                request_id=params.request.id,
                skills=params.granted_skills,
                tools=params.granted_tools,
                capability_cards=params.selected_cards,
                reason=f"CapabilityRouter 命中 {len(params.selected_hits)} 张能力卡。",
                expires_after_task=True,
            ),
        )
        _mark_capability_request_status(self, params.task.id, params.request.id, "GRANTED")
        routed_task = self.load(params.task.id)
        self._append_task_work_log(
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
        selected_cards, granted_skills, granted_tools, reasons = self._extract_selected_hits_data(
            selected_hits
        )
        if not selected_hits:
            return self._route_capability_no_hits(
                CapabilityNoHitsParams(task, request, query, hits, apply)
            )
        if not apply:
            now = time.time()
            # LLM: record construction lives in capability_route_service so this mixin stays a facade.
            return build_would_grant_record(
                WouldGrantRecordParams(
                    task=task,
                    request=request,
                    query=query,
                    hits=hits,
                    granted_skills=granted_skills,
                    granted_tools=granted_tools,
                    selected_cards=selected_cards,
                    reasons=reasons,
                    created_at=now,
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
    # LLM: manager APIs keep legacy explicit fields but normalize immediately to one options bundle.
    return SubAgentCapabilityRouteOptions(
        apply=apply,
        run_ids=run_ids,
        limit=limit,
    )
