# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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
from .capability_scope import (
    grant_command_allowlist,
    grant_tools,
    request_scope_snapshot,
    scoped_constraints,
)
from .models import CapabilityRequest, SubAgentCapabilityRouteOptions, SubAgentTask
from .policies import _capability_request_query, _select_capability_hits
from .reports import CapabilityRouteRecord, CapabilityRouteReport
from .services.lifecycle import RecordCapabilityGrantParams

# LLM: Capability routing now carries request scope into grants, but still never executes tools.
if TYPE_CHECKING:
    from ..capabilities import CapabilitySearchHit


# LLM: _iter_open_capability_requests 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理迭代开放能力requests相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _iter_open_capability_requests(tasks):
    return (
        (task, request)
        for task in tasks
        for request in task.capability_requests
        if request.status == "OPEN"
    )


# LLM: CapabilityNoHitsParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存能力nohits参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class CapabilityNoHitsParams:

    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    apply: bool


# LLM: SubAgentCapabilityMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent能力混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentCapabilityMixin:
    # LLM: _route_capability_no_hits 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理route能力nohits相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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

    # LLM: route_capability_requests 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理route能力requests相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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

    # LLM: write_capability_route_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入能力route报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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

    # LLM: _extract_selected_hits_data 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理extractselectedhitsdata相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _extract_selected_hits_data(
        self,
        selected_hits: list[CapabilitySearchHit],
    ) -> tuple[list[dict[str, str]], list[str], list[str], list[str]]:
        return extract_selected_hits_data(selected_hits)
    # LLM: _route_capability_apply 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理route能力应用相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
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
                tools=grant_tools(params.request, params.granted_tools),
                mcp_tools=params.request.requested_mcp_tools,
                command_allowlist=grant_command_allowlist(params.request),
                grant_type=params.request.capability_type,
                capability_cards=params.selected_cards,
                reason=f"CapabilityRouter 命中 {len(params.selected_hits)} 张能力卡。",
                constraints=scoped_constraints(params.request),
                path_scope=params.request.path_scope or params.request.cwd_scope,
                network_scope=params.request.network_scope,
                output_budget=params.request.output_budget,
                risk_level=params.request.risk_level,
                expires_after_task=True,
                reserved={"request_scope": request_scope_snapshot(params.request)},
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

    # LLM: _route_capability_request 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理route能力请求相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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
            # LLM: 记录构造留在能力路由服务里，本混入只维持门面职责。
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


# LLM: _capability_route_options 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理能力route选项相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
    # LLM: 管理器接口保留旧显式字段，内部立即归一成一个选项参数包。
    return SubAgentCapabilityRouteOptions(
        apply=apply,
        run_ids=run_ids,
        limit=limit,
    )
