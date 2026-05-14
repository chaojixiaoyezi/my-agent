# LLM: Due-check summary helpers keep inspection plumbing outside the board facade.
# 模块用途: 构建 due-check 设置、逐任务巡检和摘要，降低 SubAgentBoardService 的函数长度。

from __future__ import annotations

"""due-check helper functions for subagent boards."""

from typing import Any

from ..reports import DueCheckIssue
from .board_due_checks import inspect_single_task_due
from .board_due_models import DueCheckSettings, InspectTaskDueRequest
from .board_items import build_risk_flags


# LLM: due_check_settings turns capability config values into one immutable inspection bundle.
# 函数用途: 从配置里读取 heartbeat、run timeout、证据和 no-progress 限制，生成 due-check 参数包。
def due_check_settings(config: Any, now: float) -> DueCheckSettings:
    heartbeat_timeout = config.subagent_heartbeat_timeout if config else 0
    run_timeout = config.subagent_run_timeout if config else 0
    min_evidence = config.subagent_min_evidence_for_done if config else 0
    no_progress_attempt_limit = getattr(config, "subagent_no_progress_attempt_limit", 4) if config else 4
    return DueCheckSettings(
        now=now,
        heartbeat_timeout=heartbeat_timeout,
        run_timeout=run_timeout,
        min_evidence=min_evidence,
        no_progress_attempt_limit=no_progress_attempt_limit,
    )


# LLM: inspect_due_tasks walks the scoped task set and delegates per-task checks.
# 函数用途: 对筛选后的任务逐个执行 due-check，并复用 task_index 做 refs-only 父子检查。
def inspect_due_tasks(manager: Any, tasks: list[Any], settings: DueCheckSettings) -> list[DueCheckIssue]:
    issues: list[DueCheckIssue] = []
    task_index = {task.id: task for task in tasks}
    for task in tasks:
        issues.extend(
            inspect_single_task_due(
                InspectTaskDueRequest(
                    manager=manager,
                    task=task,
                    settings=settings,
                    risk_flags_builder=build_risk_flags,
                    task_index=task_index,
                )
            )
        )
    return issues


# LLM: due_check_summary counts severities and issue kinds for the public report.
# 函数用途: 把 due-check issues 聚合成稳定 summary 字段。
def due_check_summary(issues: list[DueCheckIssue]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(issues)}
    for issue in issues:
        summary[issue.severity] = summary.get(issue.severity, 0) + 1
        summary[issue.kind] = summary.get(issue.kind, 0) + 1
    return summary
