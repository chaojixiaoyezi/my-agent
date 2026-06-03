
from __future__ import annotations

"""task fact-source path helpers for memory-resume.

新手说明:
memory-resume 只负责推荐该读哪些权威文件。这里集中维护 subagent
恢复文件的优先级，避免 CLI 查询和运行时自动恢复走出不同顺序。
"""

from typing import Any


def task_recovery_read_paths(task: Any) -> list[str]:
    """Return compact-first task fact sources for resume."""

    return _dedupe_strings(
        [
            getattr(task, "checkpoint_json", ""),
            getattr(task, "status_report_json", ""),
            getattr(task, "progress_md", ""),
            getattr(task, "decision_ledger_json", ""),
            getattr(task, "failing_tests_json", ""),
            getattr(task, "next_actions_json", ""),
            getattr(task, "status_file", ""),
            getattr(task, "work_log_file", ""),
            getattr(task, "handoff_file", ""),
            getattr(task, "acceptance_file", ""),
            getattr(task, "test_checklist_file", ""),
            getattr(task, "output_json", ""),
        ]
    )


def _dedupe_strings(values: list[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
