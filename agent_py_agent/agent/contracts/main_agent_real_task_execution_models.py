# LLM: Real-task execution models share the unified task execution dataclasses.
# 模块用途: 保留 real_task 类型名和 schema 常量，执行报告结构只维护一套。

from __future__ import annotations

from .main_agent_task_execution_models import (
    MainAgentTaskExecutionCaseResult as MainAgentRealTaskExecutionCaseResult,
)
from .main_agent_task_execution_models import (
    MainAgentTaskExecutionReport as MainAgentRealTaskExecutionReport,
)
from .main_agent_task_execution_models import (
    MainAgentTaskExecutionRequest as MainAgentRealTaskExecutionRequest,
)

SCHEMA_VERSION = "main-agent-real-task-execution.v1"


__all__ = [
    "MainAgentRealTaskExecutionCaseResult",
    "MainAgentRealTaskExecutionReport",
    "MainAgentRealTaskExecutionRequest",
    "SCHEMA_VERSION",
]
