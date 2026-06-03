
from __future__ import annotations

"""due-check helper functions for subagent boards."""

from typing import Any

from ...reports import DueCheckIssue
from .due_checks import inspect_single_task_due
from .due_models import DueCheckSettings, InspectTaskDueRequest
from .items import build_risk_flags


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


def due_check_summary(issues: list[DueCheckIssue]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(issues)}
    for issue in issues:
        summary[issue.severity] = summary.get(issue.severity, 0) + 1
        summary[issue.kind] = summary.get(issue.kind, 0) + 1
    return summary
