# LLM: Real-task execution summary names are compatibility aliases over runtime summary helpers.
# 模块用途: 保留旧 real_task summary import 名称，实际逻辑统一走 main_agent_task_runtime_summary。

from __future__ import annotations

from .main_agent_task_runtime_summary import (
    concurrency_summary,
    execution_summary,
    select_cases,
)

__all__ = ["concurrency_summary", "execution_summary", "select_cases"]
