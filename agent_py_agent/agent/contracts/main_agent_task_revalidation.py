# LLM: Task revalidation is a thin wrapper over the shared runtime revalidator.
# 模块用途: 保留旧 task 复验入口，实际报告读取和产物复验走统一 runtime revalidation。

from __future__ import annotations

from pathlib import Path

from .main_agent_task_execution_models import MainAgentTaskExecutionReport
from .main_agent_task_runtime_adapters import task_runtime_adapter
from .main_agent_task_runtime_revalidation import revalidate_main_agent_task_runtime


# LLM: revalidate_main_agent_task_execution re-checks artifacts without rerunning commands.
# 函数用途: 读取已有 execution_report.json，只复验 expected artifact 合同并重写验收报告。
def revalidate_main_agent_task_execution(
    report_path: Path,
    *,
    workspace: Path | None = None,
) -> MainAgentTaskExecutionReport:
    return revalidate_main_agent_task_runtime(
        report_path,
        workspace=workspace,
        runtime_adapter=task_runtime_adapter(),
    )


__all__ = ["revalidate_main_agent_task_execution"]
