# LLM: Real task execution summary helpers keep the runner file small.
# 模块用途: 负责 case 筛选、执行汇总和并发摘要，主执行器只关心运行流程。

from __future__ import annotations

from .main_agent_task_execution_models import (
    MainAgentTaskExecutionCaseResult,
    MainAgentTaskExecutionRequest,
)
from .main_agent_task_suite import MainAgentTaskCasePlan
from .state_machine import normalize_status


# LLM: select_cases filters by structured case ids, never by prompt text.
# 函数用途: 按 case_id 选择要计划或执行的任务；空列表表示全量。
def select_cases(
    cases: list[MainAgentTaskCasePlan],
    case_ids: tuple[str, ...],
) -> list[MainAgentTaskCasePlan]:
    if not case_ids:
        return cases
    allowed = set(case_ids)
    return [case for case in cases if case.case_id in allowed]


# LLM: execution_summary counts planned and executed case statuses for CLI display.
# 函数用途: 汇总执行报告状态，方便用户快速看计划/成功/失败数量。
def execution_summary(cases: list[MainAgentTaskExecutionCaseResult]) -> dict[str, int]:
    statuses = [normalize_status(case.status) for case in cases]
    planning = sum(status == "PLANNING" for status in statuses)
    done = sum(status == "DONE" for status in statuses)
    return {
        "total": len(cases),
        "planning": planning,
        "planned": planning,
        "done": done,
        "completed": done,
        "failed": sum(status == "FAILED" for status in statuses),
    }


# LLM: concurrency_summary records effective worker limits for later real API smoke runs.
# 函数用途: 把请求并发和实际执行工位写进报告，方便观察多个主代理并发是否按合同运行。
def concurrency_summary(
    request: MainAgentTaskExecutionRequest,
    cases: list[MainAgentTaskCasePlan],
) -> dict[str, int]:
    return {
        "requested_max_workers": request.max_workers,
        "effective_max_workers": max(1, min(request.max_workers, len(cases) or 1)),
        "case_count": len(cases),
    }


__all__ = ["concurrency_summary", "execution_summary", "select_cases"]
