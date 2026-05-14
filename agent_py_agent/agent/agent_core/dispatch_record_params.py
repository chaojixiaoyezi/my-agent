# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: WorkflowRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存工作流记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class WorkflowRecordParams:
    agent: Any
    tasks: list
    workflow_mode: str
    limit: int
    apply: bool
    override_task_off: bool = False


# LLM: DryRunWorkflowRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存dryrun工作流记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DryRunWorkflowRecordParams:
    agent: Any
    task: Any
    workflow_mode: str
    preview: dict
    worker_count: int


# LLM: ActionApplyRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存动作应用记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ActionApplyRecordParams:
    agent: Any
    cfg: Any
    apply: bool
    take_over_by: str | None
    locked_files: list[str] | None
    limit: int
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None


# LLM: CapabilityRouteRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存能力route记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class CapabilityRouteRecordParams:
    agent: Any
    router: Any
    cfg: Any
    apply: bool
    limit: int


# LLM: PatchReviewRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存补丁审查记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class PatchReviewRecordParams:
    agent: Any
    patch_run_ids: list[str]
    apply: bool
    reviewer: str
    note: str
    limit: int


# LLM: AcceptanceRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存验收记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class AcceptanceRecordParams:
    agent: Any
    apply: bool
    reviewer: str
    note: str
    limit: int
    execute_acceptance_tests: bool = False
    auto_apply_acceptance_followup: bool = False
