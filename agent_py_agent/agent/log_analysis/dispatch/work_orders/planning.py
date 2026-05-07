# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

"""Planning logic for log-analysis subagent work orders.

Dataclasses live in models.py so this module can stay focused on planning flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ...agents.summaries import render_case_summary, summarize_case
from .models import (
    PARENT_FINAL_GATE,
    CreateWorkOrdersParams,
    LogAnalysisWorkOrderPlan,
    PlanInputs,
    SubagentWorkOrder,
)

DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"
PLAN_NOT_READY_ISSUE = "work-order plan is not ready; refusing to create subagent tasks"


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 PlanWorkOrdersOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 PlanWorkOrdersOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class PlanWorkOrdersOptions:
    evidence_refs: Any = None
    route_summary: Mapping[str, Any] | None = None
    quality_contract: Mapping[str, Any] | None = None
    mode: str = "manual"
    dry_run: bool = True


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _get 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 get 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a mapping or an object."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _case_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 case id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _case_id(case: Any, summary: Mapping[str, Any]) -> str:
    """Return a stable case id from case fields or summary fallback."""
    summary_case = summary.get("case", {})
    if not isinstance(summary_case, Mapping):
        summary_case = {}
    return str(_get(case, "case_id") or _get(case, "id") or summary_case.get("case_id") or "unknown-case")


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _merge_unique 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge unique 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_unique(*values: Any) -> list[str]:
    """Normalize, merge, and de-duplicate evidence refs in first-seen order."""
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        _append_new_refs(output, seen, normalize_evidence_refs(value))
    return output


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _append_new_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append new refs 相关记录，集中处理目标路径、格式化和状态更新。
def _append_new_refs(output: list[str], seen: set[str], refs: list[str]) -> None:
    for item in refs:
        if item not in seen:
            output.append(item)
            seen.add(item)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _acceptance_checks 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 acceptance checks 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _acceptance_checks(quality_contract: Mapping[str, Any] | None) -> list[str]:
    """Combine default and per-case acceptance checks."""
    checks = list(DEFAULT_ACCEPTANCE_CHECKS)
    if quality_contract:
        _append_acceptance_checks(checks, quality_contract.get("acceptance_checks", []))
    return checks


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _append_acceptance_checks 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append acceptance checks 相关记录，集中处理目标路径、格式化和状态更新。
def _append_acceptance_checks(checks: list[str], items: Any) -> None:
    for item in items:
        text = str(item or "").strip()
        if text and text not in checks:
            checks.append(text)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _build_work_order_context 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build work order context 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _build_work_order_context(
    summary: dict[str, Any],
    refs: list[str],
    route: dict[str, Any],
    quality_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the common context for analyst and reviewer work orders."""
    return {
        "case_summary": render_case_summary(
            {
                "case": summary.get("case", {}),
                "evidence": refs,
                "route": route,
            }
        ),
        "route_summary": route,
        "quality_contract": dict(quality_contract or {}),
    }


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _create_work_orders 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create work orders 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _create_work_orders(*, params: CreateWorkOrdersParams) -> tuple[SubagentWorkOrder, SubagentWorkOrder]:
    """Create analyst and reviewer work orders from common data."""
    analyst = _create_analyst_work_order(params)
    reviewer = _create_reviewer_work_order(params)
    return analyst, reviewer


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _create_analyst_work_order 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create analyst work order 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _create_analyst_work_order(params: CreateWorkOrdersParams) -> SubagentWorkOrder:
    return SubagentWorkOrder(
        role="analyst",
        case_id=params.case_id,
        goal="Prepare an evidence-backed log analysis report for reviewer gate.",
        mode=params.mode,
        dry_run=params.dry_run,
        ready=params.ready,
        allowed_tools=list(DEFAULT_ANALYST_TOOLS),
        evidence_refs=list(params.refs),
        context={
            **params.common_context,
            "handoff_contract": "AnalystInput",
            "expected_output": "AnalystReport",
        },
        acceptance_checks=list(params.checks),
        issues=list(params.issues),
        risks=list(params.risks),
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _create_reviewer_work_order 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 create reviewer work order 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _create_reviewer_work_order(params: CreateWorkOrdersParams) -> SubagentWorkOrder:
    return SubagentWorkOrder(
        role="reviewer",
        case_id=params.case_id,
        goal="Review the analyst report against evidence boundaries before parent final approval.",
        mode=params.mode,
        dry_run=params.dry_run,
        ready=params.ready,
        allowed_tools=list(DEFAULT_REVIEWER_TOOLS),
        evidence_refs=list(params.refs),
        context={
            **params.common_context,
            "handoff_contract": "ReviewerInput",
            "expected_input": "AnalystReport from analyst work order",
            "expected_output": "ReviewerDecision",
        },
        acceptance_checks=list(params.checks),
        issues=list(params.issues),
        risks=list(params.risks),
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 plan_case_subagent_work_orders 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 plan case subagent work orders 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def plan_case_subagent_work_orders(
    case: Any,
    *,
    options: PlanWorkOrdersOptions | None = None,
    evidence_refs: Any = None,
    route_summary: Mapping[str, Any] | None = None,
    quality_contract: Mapping[str, Any] | None = None,
    mode: str = "manual",
    dry_run: bool = True,
) -> LogAnalysisWorkOrderPlan:
    """Generate bounded analyst/reviewer work orders for one log-analysis case."""
    plan_options = options or PlanWorkOrdersOptions(
        evidence_refs=evidence_refs,
        route_summary=route_summary,
        quality_contract=quality_contract,
        mode=str(mode),
        dry_run=bool(dry_run),
    )
    inputs = _plan_inputs(
        case,
        plan_options.evidence_refs,
        plan_options.route_summary,
        plan_options.quality_contract,
    )
    common_context = _build_work_order_context(
        inputs.summary,
        inputs.refs,
        inputs.route,
        plan_options.quality_contract,
    )
    analyst, reviewer = _create_work_orders(
        params=CreateWorkOrdersParams(
            case_id=inputs.case_id,
            common_context=common_context,
            refs=inputs.refs,
            checks=inputs.checks,
            issues=inputs.issues,
            risks=inputs.risks,
            mode=plan_options.mode,
            dry_run=plan_options.dry_run,
            ready=inputs.ready,
        )
    )
    return _plan_from_orders(inputs, [analyst, reviewer], mode=plan_options.mode, dry_run=plan_options.dry_run)


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _plan_from_orders 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 plan from orders 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def _plan_from_orders(
    inputs: PlanInputs,
    work_orders: list[SubagentWorkOrder],
    *,
    mode: str,
    dry_run: bool,
) -> LogAnalysisWorkOrderPlan:
    return LogAnalysisWorkOrderPlan(
        case_id=inputs.case_id,
        ready=inputs.ready,
        dry_run=dry_run,
        mode=mode,
        work_orders=work_orders,
        issues=inputs.issues,
        risks=inputs.risks,
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _plan_inputs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 plan inputs 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def _plan_inputs(
    case: Any,
    evidence_refs: Any,
    route_summary: Mapping[str, Any] | None,
    quality_contract: Mapping[str, Any] | None,
) -> PlanInputs:
    summary = summarize_case(case).to_dict()
    refs = _merge_unique(evidence_refs, _get(case, "evidence_refs") or _get(case, "evidence"), summary.get("evidence"))
    issues, risks = _readiness_notes(refs)
    return PlanInputs(
        summary=summary,
        case_id=_case_id(case, summary),
        route=dict(route_summary or summary.get("route") or {}),
        refs=refs,
        checks=_acceptance_checks(quality_contract),
        issues=issues,
        risks=risks,
        ready=bool(refs),
    )


# LLM: dispatch 流程按预算、队列和工单状态分派日志分析任务；修改 _readiness_notes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 readiness notes 在当前模块中的核心转换或协调步骤，衔接 dispatch 流程按预算、队列和工单状态分派日志分析任务。
def _readiness_notes(refs: list[str]) -> tuple[list[str], list[str]]:
    if refs:
        return [], []
    return [NO_EVIDENCE_ISSUE], ["dispatching without evidence_refs would invite unsupported analysis"]


build_log_analysis_work_orders = plan_case_subagent_work_orders

__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "NO_EVIDENCE_ISSUE",
    "PARENT_FINAL_GATE",
    "PLAN_NOT_READY_ISSUE",
    "LogAnalysisWorkOrderPlan",
    "PlanWorkOrdersOptions",
    "SubagentWorkOrder",
    "build_log_analysis_work_orders",
    "plan_case_subagent_work_orders",
]
