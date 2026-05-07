# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""task fact-source path helpers for memory-resume.

新手说明:
memory-resume 只负责推荐该读哪些权威文件。这里集中维护 subagent
恢复文件的优先级，避免 CLI 查询和运行时自动恢复走出不同顺序。
"""

from typing import Any


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 task_recovery_read_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 task recovery read paths 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def task_recovery_read_paths(task: Any) -> list[str]:
    """Return compact-first task fact sources for resume."""

    return _dedupe_strings(
        [
            # LLM: checkpoint artifacts summarize recovery facts without full chat history.
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _dedupe_strings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe strings 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe_strings(values: list[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
