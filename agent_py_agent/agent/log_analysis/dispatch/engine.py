# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Local dispatch engine for log-analysis analyst work."""

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..agents.contracts import normalize_evidence_refs, review_analyst_report
from ..agents.summaries import render_case_summary, summarize_case
from .budgets import DispatchBudget
from .health import build_health_summary
from .queue import DispatchRequest, InvestigationQueue


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class DispatchResult:
    request: DispatchRequest
    dispatched: bool
    reason: str
    agent_id: str = ""

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 case_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 case id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    @property
    def case_id(self) -> str:
        return self.request.case_id

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 status 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 status 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    @property
    def status(self) -> str:
        return self.request.status

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 run_id 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 推进 run id 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
    @property
    def run_id(self) -> str:
        return self.agent_id or self.request.request_id

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 message 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 message 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    @property
    def message(self) -> str:
        return self.reason

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 metadata 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 metadata 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "request_id": self.request.request_id,
            "dispatched": self.dispatched,
            "agent_id": self.agent_id,
            "priority": self.request.priority,
            "evidence_refs": list(self.request.evidence_refs),
        }

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["request"] = self.request.to_dict()
        payload.update(
            {
                "case_id": self.case_id,
                "status": self.status,
                "run_id": self.run_id,
                "message": self.message,
                "metadata": self.metadata,
            }
        )
        return payload


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _get 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _case_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 case id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _case_id(case: Any, summary: dict[str, Any]) -> str:
    return str(
        _get(case, "case_id")
        or _get(case, "id")
        or summary.get("case", {}).get("case_id")
        or "unknown-case"
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _priority 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 priority 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _priority(case: Any, summary: dict[str, Any]) -> str:
    return str(
        _get(case, "priority")
        or _get(case, "severity")
        or summary.get("case", {}).get("priority")
        or summary.get("case", {}).get("severity")
        or ""
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _evidence_refs_from_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 evidence refs from summary 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _evidence_refs_from_summary(case: Any, summary: dict[str, Any]) -> list[str]:
    refs = normalize_evidence_refs(_get(case, "evidence_refs") or _get(case, "evidence"))
    if refs:
        return refs
    return normalize_evidence_refs(summary.get("evidence"))


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchEngine 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 协调 DispatchEngine 的依赖和流程，把多步读取、校验或调度收束成稳定接口。
class DispatchEngine:
    """Create dispatch requests without making the parent session do analysis."""

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(
        self,
        *,
        queue: InvestigationQueue | None = None,
        budget: DispatchBudget | Mapping[str, Any] | None = None,
    ):
        self.queue = queue or InvestigationQueue()
        self.budget = budget if isinstance(budget, DispatchBudget) else DispatchBudget.from_mapping(budget)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 submit_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 submit case 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def submit_case(self, case: Any) -> DispatchResult:
        """Add a case to the queue and dispatch only when budget allows."""

        summary_obj = summarize_case(case)
        summary = summary_obj.to_dict()
        case_id = _case_id(case, summary)
        priority = _priority(case, summary)
        evidence_refs = _evidence_refs_from_summary(case, summary)
        decision = self.budget.can_dispatch_analyst(
            priority=priority,
            active_analyst_agents=len(self.queue.active(role="analyst")),
            analyst_dispatches_last_hour=len(
                self.queue.dispatched_since(time.time() - 3600, role="analyst")
            ),
        )

        request = self.queue.add_pending(
            case_id=case_id,
            priority=priority,
            reason=decision.reason if not decision.allowed else "pending dispatch",
            evidence_refs=evidence_refs,
            case_summary=summary.get("case", {}),
            route_summary=summary.get("route", {}),
        )

        if not decision.allowed:
            return DispatchResult(request=request, dispatched=False, reason=decision.reason)

        agent_id = f"analyst-{request.request_id}"
        self.queue.mark_dispatched(request.request_id, agent_id=agent_id)
        return DispatchResult(request=request, dispatched=True, reason="dispatched", agent_id=agent_id)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 enqueue_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 推进 enqueue case 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
    def enqueue_case(
        self,
        case: Any,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> DispatchResult:
        """Public protocol alias for submit_case.

        The current local engine does not need context to enqueue a case, but
        accepting it keeps the pluggable interface stable."""

        _ = context
        return self.submit_case(case)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 health 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 health 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def health(self) -> dict[str, Any]:
        return build_health_summary(queue=self.queue, budget=self.budget).to_dict()

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 build_analyst_input 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 组装 build analyst input 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
    def build_analyst_input(self, request: DispatchRequest) -> dict[str, Any]:
        """Return the small object sent to an analyst subagent."""

        return {
            "case_id": request.case_id,
            "case_summary": render_case_summary(
                {
                    "case": request.case_summary,
                    "evidence": request.evidence_refs,
                    "route": request.route_summary,
                }
            ),
            "evidence_refs": list(request.evidence_refs),
            "route_summary": dict(request.route_summary),
            "available_tools": [
                "security_query",
                "security_hunt_ip",
                "security_trace_case",
                "evidence_read",
            ],
            "budget": {
                "max_case_rounds": self.budget.max_case_rounds,
                "timeout_seconds": self.budget.analyst_agent_timeout_seconds,
            },
        }

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 review_report 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 review report 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def review_report(
        self,
        report: Any,
        *,
        known_evidence_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        return review_analyst_report(report, known_evidence_refs=known_evidence_refs).to_dict()
