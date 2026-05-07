# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Small case scheduler for detector output."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import CaseRecord, Finding
from .case_store import CaseStore


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 ScheduleResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ScheduleResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class ScheduleResult:
    recorded_findings: list[str] = field(default_factory=list)
    low_confidence_findings: list[str] = field(default_factory=list)
    cases: list[CaseRecord] = field(default_factory=list)

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_findings": list(self.recorded_findings),
            "low_confidence_findings": list(self.low_confidence_findings),
            "case_ids": [case.case_id for case in self.cases],
            "cases": [case.to_dict() for case in self.cases],
        }


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 ScheduleFindingsOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ScheduleFindingsOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ScheduleFindingsOptions:
    root: str | Path | None = None
    store: CaseStore | None = None
    min_case_confidence: float = 0.6
    merge_window_minutes: int = 15
    search_store: Any | None = None


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 CaseScheduler 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 协调 CaseScheduler 的依赖和流程，把多步读取、校验或调度收束成稳定接口。
class CaseScheduler:
    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, store: CaseStore, *, min_case_confidence: float | None = None) -> None:
        self.store = store
        self.min_case_confidence = min_case_confidence

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 schedule 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 schedule 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
    def schedule(self, findings: Sequence[Finding | Mapping[str, Any]]) -> ScheduleResult:
        result = ScheduleResult()
        seen_cases: set[str] = set()
        original_threshold = self.store.min_case_confidence
        if self.min_case_confidence is not None:
            self.store.min_case_confidence = self.min_case_confidence
        try:
            for item in findings:
                _record_scheduled_finding(self.store, result, seen_cases, item)
        finally:
            self.store.min_case_confidence = original_threshold
        return result


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _record_scheduled_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 record scheduled finding 相关记录，集中处理目标路径、格式化和状态更新。
def _record_scheduled_finding(
    store: CaseStore,
    result: ScheduleResult,
    seen_cases: set[str],
    item: Finding | Mapping[str, Any],
) -> None:
    finding = item if isinstance(item, Finding) else Finding.from_dict(item)
    result.recorded_findings.append(finding.finding_id)
    case = store.record_finding(finding)
    if case is None:
        result.low_confidence_findings.append(finding.finding_id)
        return
    if case.case_id not in seen_cases:
        seen_cases.add(case.case_id)
        result.cases.append(case)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 schedule_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 schedule findings 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def schedule_findings(
    findings: Sequence[Finding | Mapping[str, Any]],
    *,
    options: ScheduleFindingsOptions | None = None,
    root: str | Path | None = None,
    store: CaseStore | None = None,
    min_case_confidence: float = 0.6,
    merge_window_minutes: int = 15,
    search_store: Any | None = None,
) -> ScheduleResult:
    schedule_options = options or ScheduleFindingsOptions(
        root=root,
        store=store,
        min_case_confidence=float(min_case_confidence),
        merge_window_minutes=int(merge_window_minutes),
        search_store=search_store,
    )
    case_store = schedule_options.store or CaseStore(
        schedule_options.root or Path("data") / "log_analysis",
        min_case_confidence=schedule_options.min_case_confidence,
        merge_window_minutes=schedule_options.merge_window_minutes,
        search_store=schedule_options.search_store,
    )
    return CaseScheduler(case_store).schedule(findings)


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 findings_to_cases 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 findings to cases 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def findings_to_cases(
    findings: Sequence[Finding | Mapping[str, Any]],
    *,
    options: ScheduleFindingsOptions | None = None,
    root: str | Path | None = None,
    store: CaseStore | None = None,
    min_case_confidence: float = 0.6,
    merge_window_minutes: int = 15,
    search_store: Any | None = None,
) -> list[CaseRecord]:
    return schedule_findings(
        findings,
        options=options,
        root=root,
        store=store,
        min_case_confidence=min_case_confidence,
        merge_window_minutes=merge_window_minutes,
        search_store=search_store,
    ).cases


__all__ = ["CaseScheduler", "ScheduleFindingsOptions", "ScheduleResult", "findings_to_cases", "schedule_findings"]
