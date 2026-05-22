# LLM: Main-agent task execution is now a thin adapter over the shared runtime.
# 模块用途: 保留通用任务执行入口和旧私有测试钩子，实际流程由 main_agent_task_runtime 统一执行。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .main_agent_task_execution_models import (
    MainAgentTaskExecutionCaseResult,
    MainAgentTaskExecutionReport,
    MainAgentTaskExecutionRequest,
)
from .main_agent_task_execution_state import CaseRuntime
from .main_agent_task_revalidation import revalidate_main_agent_task_execution
from .main_agent_task_runtime import (
    completed_case_result,
    prepare_or_execute_case,
    run_case,
    run_main_agent_task_runtime,
    timeout_case_result,
)
from .main_agent_task_runtime_adapters import task_runtime_adapter
from .main_agent_task_subprocess import (
    TaskRunSubprocessResult,
    run_task_subprocess,
)


# LLM: run_main_agent_task_execution is the public controlled runner entrypoint.
# 函数用途: 生成任务计划和隔离运行文件；execute=True 时受控启动 my-agent run。
def run_main_agent_task_execution(
    request: MainAgentTaskExecutionRequest,
) -> MainAgentTaskExecutionReport:
    return run_main_agent_task_runtime(request, runtime_adapter=_runtime_adapter())


# LLM: _runtime_adapter binds legacy task modules to the common runtime contract.
# 函数用途: 用结构化函数/模型引用描述 task 轨道差异，避免复制执行状态机。
def _runtime_adapter():
    return task_runtime_adapter(run_task_subprocess)


# LLM: Legacy private hooks delegate to the shared runtime for focused tests.
# 函数用途: 保留旧测试/外部调试入口，但不再让双轨各自维护实现。
def _prepare_or_execute(case: Any, request: MainAgentTaskExecutionRequest, *, workspace: Path) -> Any:
    return prepare_or_execute_case(case, request, workspace=workspace, runtime_adapter=_runtime_adapter())


def _run_case(runtime: CaseRuntime) -> MainAgentTaskExecutionCaseResult:
    return run_case(runtime, runtime_adapter=_runtime_adapter())


def _completed_case_result(runtime: CaseRuntime, process: TaskRunSubprocessResult) -> MainAgentTaskExecutionCaseResult:
    return completed_case_result(runtime, process, runtime_adapter=_runtime_adapter())


def _timeout_case_result(runtime: CaseRuntime, process: TaskRunSubprocessResult) -> MainAgentTaskExecutionCaseResult:
    return timeout_case_result(runtime, process, runtime_adapter=_runtime_adapter())


__all__ = [
    "MainAgentTaskExecutionCaseResult",
    "MainAgentTaskExecutionReport",
    "MainAgentTaskExecutionRequest",
    "revalidate_main_agent_task_execution",
    "run_main_agent_task_execution",
]
