# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""helper dataclasses and functions for capability route logging and status updates.

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


# LLM: RouteCapabilityGrantParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存route能力grant参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: RouteCapabilityApplyParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存route能力应用参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
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


# LLM: RouteCapabilityGapParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存route能力缺口参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RouteCapabilityGapParams:
    """Params bundle for _route_capability_gap."""

    task: SubAgentTask
    request: CapabilityRequest
    query: str
    hits: list
    gap: object


# LLM: _route_capability_gap 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理route能力缺口相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _route_capability_grant 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理route能力grant相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _mark_capability_request_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新能力请求状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _mark_capability_request_status(manager, run_id: str, request_id: str, status: str) -> None:
    """更新 capability request 状态。"""
    task = manager.load(run_id)
    for request in task.capability_requests:
        if request.id == request_id:
            request.status = status
    task.updated_at = time.time()
    manager.save(task)


# LLM: _append_capability_route_log 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 写入能力routelog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
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
