# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Dispatch budget rules for log-analysis subagents."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 BudgetDecision 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 BudgetDecision 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 DispatchBudget 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DispatchBudget 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DispatchBudget:
    """Local dispatch budget.

    Zero means disabled for worker counts, auto dispatch, and hourly budget."""

    case_auto_dispatch_enabled: bool = False
    max_parallel_analyst_agents: int = 0
    analyst_agent_budget_per_hour: int = 0
    analyst_agent_timeout_seconds: int = 0
    p0_auto_dispatch_enabled: bool = False
    p1_auto_dispatch_enabled: bool = False
    max_case_rounds: int = 1

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 from_mapping 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from mapping 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> DispatchBudget:
        if not payload:
            return cls()
        hourly_budget = payload.get(
            "analyst_agent_budget_per_hour",
            payload.get("dispatch_budget_per_hour", 0),
        )
        auto_dispatch = payload.get(
            "case_auto_dispatch_enabled",
            payload.get("auto_dispatch_enabled", False),
        )
        return cls(
            case_auto_dispatch_enabled=bool(auto_dispatch),
            max_parallel_analyst_agents=max(0, int(payload.get("max_parallel_analyst_agents", 0) or 0)),
            analyst_agent_budget_per_hour=max(0, int(hourly_budget or 0)),
            analyst_agent_timeout_seconds=max(0, int(payload.get("analyst_agent_timeout_seconds", 0) or 0)),
            p0_auto_dispatch_enabled=bool(payload.get("p0_auto_dispatch_enabled", False)),
            p1_auto_dispatch_enabled=bool(payload.get("p1_auto_dispatch_enabled", False)),
            max_case_rounds=max(0, int(payload.get("max_case_rounds", 1) or 0)),
        )

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 auto_dispatch_enabled_for_priority 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 auto dispatch enabled for priority 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def auto_dispatch_enabled_for_priority(self, priority: str = "") -> bool:
        normalized = str(priority or "").upper()
        if self.case_auto_dispatch_enabled:
            return True
        if normalized == "P0" and self.p0_auto_dispatch_enabled:
            return True
        if normalized == "P1" and self.p1_auto_dispatch_enabled:
            return True
        return False

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 can_dispatch_analyst 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 can dispatch analyst 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
    def can_dispatch_analyst(
        self,
        *,
        priority: str = "",
        active_analyst_agents: int = 0,
        analyst_dispatches_last_hour: int = 0,
    ) -> BudgetDecision:
        if self.max_parallel_analyst_agents <= 0:
            return BudgetDecision(False, "max_parallel_analyst_agents=0 disables analyst dispatch")
        if not self.auto_dispatch_enabled_for_priority(priority):
            return BudgetDecision(False, "case auto dispatch is disabled for this priority")
        if active_analyst_agents >= self.max_parallel_analyst_agents:
            return BudgetDecision(False, "max parallel analyst budget exhausted")
        if self.analyst_agent_budget_per_hour <= 0:
            return BudgetDecision(False, "analyst_agent_budget_per_hour=0 disables analyst dispatch")
        if analyst_dispatches_last_hour >= self.analyst_agent_budget_per_hour:
            return BudgetDecision(False, "hourly analyst dispatch budget exhausted")
        return BudgetDecision(True, "dispatch allowed by budget")
