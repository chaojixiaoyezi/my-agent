# LLM: Real-task runtime issue names are compatibility aliases over shared issue-code helpers.
# 模块用途: 保留旧 real_task runtime issue import 名称，实际逻辑统一走 main_agent_task_runtime_issue_codes。

from __future__ import annotations

from .main_agent_task_runtime_issue_codes import (
    case_issue_codes,
    line_repetition_ratio,
    output_diagnostic_issue_codes,
)

__all__ = ["case_issue_codes", "line_repetition_ratio", "output_diagnostic_issue_codes"]
