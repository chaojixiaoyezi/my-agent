# LLM: Real task execution state bundles keep the main runner file small.
# 模块用途: 保存真实任务执行期间的内部 runtime/result bundle，避免执行器继续膨胀。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .main_agent_real_task_acceptance import RealTaskAcceptanceReport
from .main_agent_real_task_execution_models import MainAgentRealTaskExecutionRequest
from .main_agent_real_task_suite import MainAgentRealTaskCasePlan


# LLM: CaseRuntime bundles all immutable facts needed to execute one planned case.
# 类用途: 聚合单个真实任务的计划、请求、路径、命令和工作区，避免内部 helper 参数散落。
@dataclass(frozen=True)
class CaseRuntime:
    case: MainAgentRealTaskCasePlan
    request: MainAgentRealTaskExecutionRequest
    paths: dict[str, Path]
    command: list[str]
    workspace: Path


# LLM: CaseResultBundle bundles status facts before converting them into the public report.
# 类用途: 描述单个任务的执行结果输入，减少 helper 参数数量并保持报告字段结构化。
@dataclass
class CaseResultBundle:
    runtime: CaseRuntime
    status: str
    acceptance: RealTaskAcceptanceReport | None = None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    issues: tuple[str, ...] = field(default_factory=tuple)
    recovery_packet_ref: str = ""


__all__ = ["CaseResultBundle", "CaseRuntime"]
