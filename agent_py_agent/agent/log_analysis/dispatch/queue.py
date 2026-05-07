# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""In-memory investigation queue for local log-analysis dispatch."""

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

PENDING_INVESTIGATION = "PENDING_INVESTIGATION"
QUEUED = "QUEUED"
DISPATCHED = "DISPATCHED"
AWAITING_REVIEW = "AWAITING_REVIEW"
REVIEWED = "REVIEWED"
REJECTED = "REJECTED"
FAILED = "FAILED"

ACTIVE_STATUSES = {QUEUED, DISPATCHED, AWAITING_REVIEW}


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _new_request_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 new request id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _new_request_id() -> str:
    return f"logdisp-{uuid.uuid4().hex[:12]}"


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DispatchRequest:
    case_id: str
    role: str = "analyst"
    priority: str = ""
    status: str = PENDING_INVESTIGATION
    reason: str = ""
    request_id: str = field(default_factory=_new_request_id)
    evidence_refs: list[str] = field(default_factory=list)
    case_summary: dict[str, Any] = field(default_factory=dict)
    route_summary: dict[str, Any] = field(default_factory=dict)
    assigned_agent_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 PendingInvestigationInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 PendingInvestigationInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class PendingInvestigationInput:
    # LLM: Queue inserts keep optional legacy fields behind a single typed bundle.
    case_id: str
    priority: str = ""
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    case_summary: dict[str, Any] = field(default_factory=dict)
    route_summary: dict[str, Any] = field(default_factory=dict)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 InvestigationQueue 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 InvestigationQueue 的状态和协作方法，作为当前模块对外复用的领域对象。
class InvestigationQueue:
    """Small queue owned by the parent/session health layer."""

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, requests: list[DispatchRequest] | None = None):
        self._requests: list[DispatchRequest] = list(requests or [])

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 __len__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 实现 __len__ 协议入口，让对象能按 Python 约定参与迭代、计数或上下文管理。
    def __len__(self) -> int:
        return len(self._requests)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 requests 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 requests 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    @property
    def requests(self) -> list[DispatchRequest]:
        return list(self._requests)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 add 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 add 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def add(self, request: DispatchRequest) -> DispatchRequest:
        request.updated_at = time.time()
        self._requests.append(request)
        return request

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 add_pending 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 add pending 相关记录，集中处理目标路径、格式化和状态更新。
    def add_pending(
        self,
        *,
        params: PendingInvestigationInput | None = None,
        case_id: str = "",
        priority: str = "",
        reason: str = "",
        evidence_refs: list[str] | None = None,
        case_summary: dict[str, Any] | None = None,
        route_summary: dict[str, Any] | None = None,
    ) -> DispatchRequest:
        if params is None:
            params = PendingInvestigationInput(
                case_id=str(case_id),
                priority=str(priority),
                reason=str(reason),
                evidence_refs=list(evidence_refs or []),
                case_summary=dict(case_summary or {}),
                route_summary=dict(route_summary or {}),
            )
        return self.add(
            DispatchRequest(
                case_id=params.case_id,
                priority=params.priority,
                reason=params.reason,
                evidence_refs=list(params.evidence_refs),
                case_summary=dict(params.case_summary),
                route_summary=dict(params.route_summary),
            )
        )

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 get 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def get(self, request_id: str) -> DispatchRequest | None:
        for request in self._requests:
            if request.request_id == request_id:
                return request
        return None

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 for_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 for case 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def for_case(self, case_id: str) -> list[DispatchRequest]:
        return [request for request in self._requests if request.case_id == case_id]

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 pending 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 pending 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def pending(self, *, role: str | None = None) -> list[DispatchRequest]:
        return [
            request
            for request in self._requests
            if request.status == PENDING_INVESTIGATION and (role is None or request.role == role)
        ]

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 active 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 active 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def active(self, *, role: str | None = None) -> list[DispatchRequest]:
        return [
            request
            for request in self._requests
            if request.status in ACTIVE_STATUSES and (role is None or request.role == role)
        ]

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 dispatched_since 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 dispatched since 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def dispatched_since(self, since_timestamp: float, *, role: str | None = None) -> list[DispatchRequest]:
        counted_statuses = ACTIVE_STATUSES | {REVIEWED, REJECTED, FAILED}
        return [
            request
            for request in self._requests
            if request.status in counted_statuses
            and request.updated_at >= since_timestamp
            and (role is None or request.role == role)
        ]

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 mark_dispatched 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 mark dispatched 相关记录，集中处理目标路径、格式化和状态更新。
    def mark_dispatched(self, request_id: str, *, agent_id: str) -> DispatchRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError(request_id)
        request.status = DISPATCHED
        request.assigned_agent_id = agent_id
        request.reason = "dispatched"
        request.attempts += 1
        request.updated_at = time.time()
        return request

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 mark_rejected 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 mark rejected 相关记录，集中处理目标路径、格式化和状态更新。
    def mark_rejected(self, request_id: str, *, reason: str) -> DispatchRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError(request_id)
        request.status = REJECTED
        request.reason = reason
        request.updated_at = time.time()
        return request

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 counts_by_status 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 counts by status 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for request in self._requests:
            counts[request.status] = counts.get(request.status, 0) + 1
        return counts

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 agent_backlog 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 agent backlog 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def agent_backlog(self) -> dict[str, Any]:
        by_status = self.counts_by_status()
        return {
            "total": len(self._requests),
            "pending_investigation": by_status.get(PENDING_INVESTIGATION, 0),
            "queued": by_status.get(QUEUED, 0),
            "dispatched": by_status.get(DISPATCHED, 0),
            "awaiting_review": by_status.get(AWAITING_REVIEW, 0),
            "rejected": by_status.get(REJECTED, 0),
            "active_analyst_agents": len(self.active(role="analyst")),
        }

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.agent_backlog(),
            "requests": [request.to_dict() for request in self._requests],
        }
