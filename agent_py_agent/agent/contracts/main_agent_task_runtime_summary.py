# LLM: Main-agent task runtime summary helpers are shared by task and real_task.
# 模块用途: 统一 case 筛选、状态汇总和并发摘要，避免两个轨道维护同一逻辑。

from __future__ import annotations

from typing import Any

from .state_machine import normalize_status


# LLM: select_cases filters by structured case ids, never by prompt text.
# 函数用途: 按 case_id 选择要计划或执行的任务；空列表表示全量。
def select_cases(cases: list[Any], case_ids: tuple[str, ...]) -> list[Any]:
    if not case_ids:
        return cases
    allowed = set(case_ids)
    return [case for case in cases if case.case_id in allowed]


# LLM: execution_summary counts planned and executed case statuses.
# 函数用途: 汇总执行报告状态，方便用户快速看计划/成功/失败数量。
def execution_summary(cases: list[Any]) -> dict[str, int]:
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


# LLM: concurrency_summary records requested and effective worker limits.
# 函数用途: 把请求并发和实际执行工位写进报告，方便观察多个主代理并发是否按合同运行。
def concurrency_summary(request: Any, cases: list[Any]) -> dict[str, int]:
    return {
        "requested_max_workers": request.max_workers,
        "effective_max_workers": max(1, min(request.max_workers, len(cases) or 1)),
        "case_count": len(cases),
    }


__all__ = ["concurrency_summary", "execution_summary", "select_cases"]
