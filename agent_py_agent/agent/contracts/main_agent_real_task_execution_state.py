# LLM: Real-task execution state reuses the unified task runtime state records.
# 模块用途: 保留 real_task 导入名；case runtime/bundle 不再双轨维护。

from __future__ import annotations

from .main_agent_task_execution_state import CaseResultBundle, CaseRuntime

__all__ = ["CaseResultBundle", "CaseRuntime"]
