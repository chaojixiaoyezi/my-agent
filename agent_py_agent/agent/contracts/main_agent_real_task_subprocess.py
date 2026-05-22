# LLM: Real-task subprocess public names are compatibility aliases over the shared runner.
# 模块用途: 保留旧 real_task subprocess import 名称，实际执行统一走 main_agent_task_runtime_subprocess。

from __future__ import annotations

from .main_agent_task_runtime_subprocess import (
    TaskRuntimeSubprocessRequest,
    TaskRuntimeSubprocessResult,
    run_task_runtime_subprocess,
)

RealTaskSubprocessRequest = TaskRuntimeSubprocessRequest
RealTaskSubprocessResult = TaskRuntimeSubprocessResult
run_real_task_subprocess = run_task_runtime_subprocess

__all__ = [
    "RealTaskSubprocessRequest",
    "RealTaskSubprocessResult",
    "run_real_task_subprocess",
]
