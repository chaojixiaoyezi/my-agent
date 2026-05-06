from __future__ import annotations

import time
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
from .models import CapabilityRequest, SubAgentTask
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


class SubAgentCapabilityMixin:
    def _route_capability_no_hits(self, task, request, query, hits, apply):
        now = time.time()
        if not apply:
            return build_would_gap_record(task, request, query=query, hits=hits, created_at=now)
        return record_capability_route_gap(self, task, request, query=query, hits=hits, created_at=now)
    def route_capability_requests(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        """把 OPEN capability request 路由到 skill/tool card。
        默认 dry-run，只展示会下发哪些能力。
        `apply=True` 时才会真正生成 capability grant 或 capability gap。
        """
        cfg = config or CapabilityConfig()
        records: list[CapabilityRouteRecord] = []
        selected_runs = self._select_runs(run_ids)
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
                    apply=apply,
                )
            )
            if limit > 0 and len(records) >= limit:
                break
        return build_capability_route_report(records, apply=apply)

    def write_capability_route_report(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        report = self.route_capability_requests(
            router,
            config,
            apply=apply,
            run_ids=run_ids,
            limit=limit,
        )
        write_capability_route_report_files(self, report, apply=apply)
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
            return self._route_capability_no_hits(task, request, query, hits, apply)
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
