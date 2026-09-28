# LLM: 本服务只路由 OPEN 申请，先校验 owner 路径范围；显式包申请保留给原直属父级裁决，
# 不能用语义候选替代完整包引用。修改时同步核对 resolve、任务快照与能力申请组件测试。
# 模块用途: 为子代理匹配工具/技能；越界路径记缺口，需要父级裁决的申请保持原状态。
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
    direct_parent_tool_authority,
    effective_request_path_scope,
    existing_delete_trash_grant,
    partition_capability_paths_by_owner,
    request_scope_snapshot,
    requested_capability_tool_names,
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


# LLM: 路由复用 manager 的 canonical request/grant；只有原 apply 分支会写状态，待父级裁决记录不写账。
# 类用途: 汇总子代理的开放能力申请，生成观察报告或应用既有工具、技能授权。
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

    # LLM: owner 路径裁决先于包申请、历史授权和语义候选；包与其它能力的混合申请必须整条
    # 留给父级 resolve，避免局部自动授权提前结清请求。保持父级快照解析和 grant 持久化为原权威。
    # 函数用途: 路由一条申请；路径越界可记缺口，显式包申请只报告待裁决，其余走原匹配链。
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
        if any(reference.startswith("capability:") for reference in request.requested_skills or []):
            # 前缀只标识包命名域，不证明父级拥有该包；可用性和完整引用由原 resolve 校验。
            return _parent_resolution_record(task, request, query, hits)
        requested_tool_names = requested_capability_tool_names(request)
        if requested_tool_names:
            parent_authority = direct_parent_tool_authority(
                self.manager,
                task,
                requested_tool_names,
            )
            if not parent_authority.unavailable_tools:
                # 直属父级已经持有全部 exact 工具时，OPEN request 必须留给父级模型裁决；
                # 语义 CapabilityRouter 没有匹配 card 不能抢先把它关闭成 GAP。
                return _parent_resolution_record(task, request, query, hits, parent_authority)
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


# LLM: 即使 apply=True，本记录也不写申请或授权；包前缀不代表可授权，不能伪造工具权限事实。
# 函数用途: 说明原申请正在等待直属父级 grant/deny，保留已有 OPEN 与唤醒链，避免语义路由抢先结清。
def _parent_resolution_record(
    task: SubAgentTask,
    request: CapabilityRequest,
    query: str,
    hits: list[CapabilitySearchHit],
    authority: object | None = None,
) -> CapabilityRouteRecord:
    request_scope = {"capability_request": request_scope_snapshot(request)}
    message = "申请包含显式能力包引用；保留 OPEN，等待直属父级按当前快照结构化 grant/deny。"
    if authority is not None:
        request_scope["parent_tool_authority"] = authority.to_dict()
        message = "申请工具均在直属父级当前权限内；保留 OPEN，等待直属父级结构化 grant/deny。"
    return CapabilityRouteRecord(
        id=f"parent-resolution:{request.id}",
        run_id=task.id,
        request_id=request.id,
        status="PARENT_RESOLUTION_REQUIRED",
        dry_run=True,
        query=query,
        candidate_count=len(hits),
        request_scope=request_scope,
        message=message,
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
