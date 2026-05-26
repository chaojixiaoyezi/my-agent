# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Data structures for log-analysis subagent work-order planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 SubagentWorkOrder 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SubagentWorkOrder 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class SubagentWorkOrder:
    """Work-order card passed to an analyst or reviewer subagent."""

    role: str
    case_id: str
    goal: str
    mode: str = "manual"
    dry_run: bool = True
    ready: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        """Return a stable serializable work-order payload."""
        return asdict(self)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 LogAnalysisWorkOrderPlan 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 LogAnalysisWorkOrderPlan 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class LogAnalysisWorkOrderPlan:
    """Dry-run plan containing the case work orders and readiness state."""

    case_id: str
    ready: bool
    dry_run: bool = True
    mode: str = "manual"
    work_orders: list[SubagentWorkOrder] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    # LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        """Return the complete serializable plan snapshot."""
        payload = asdict(self)
        payload["work_orders"] = [order.to_dict() for order in self.work_orders]
        return payload


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 CreateWorkOrdersParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CreateWorkOrdersParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CreateWorkOrdersParams:
    """Params bundle for building the analyst and reviewer work orders."""

    case_id: str
    common_context: dict[str, Any]
    refs: list[str]
    checks: list[str]
    issues: list[str]
    risks: list[str]
    mode: str
    dry_run: bool
    ready: bool


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 PlanInputs 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 PlanInputs 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class PlanInputs:
    """Normalized inputs used to build a work-order plan."""

    summary: dict[str, Any]
    case_id: str
    route: dict[str, Any]
    refs: list[str]
    checks: list[str]
    issues: list[str]
    risks: list[str]
    ready: bool


__all__ = [
    "CreateWorkOrdersParams",
    "LogAnalysisWorkOrderPlan",
    "PlanInputs",
    "SubagentWorkOrder",
]
