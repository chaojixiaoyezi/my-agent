# LLM: Task subprocess public names are compatibility aliases over the shared runtime runner.
# 模块用途: 保留旧 task subprocess import 名称，实际执行统一走 main_agent_task_runtime_subprocess。

from __future__ import annotations

from .main_agent_task_runtime_subprocess import (
    TaskRuntimeSubprocessRequest,
    TaskRuntimeSubprocessResult,
    run_task_runtime_subprocess,
)

TaskRunSubprocessRequest = TaskRuntimeSubprocessRequest
TaskRunSubprocessResult = TaskRuntimeSubprocessResult
run_task_subprocess = run_task_runtime_subprocess

__all__ = [
    "TaskRunSubprocessRequest",
    "TaskRunSubprocessResult",
    "run_task_subprocess",
]
