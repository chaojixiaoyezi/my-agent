# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


# LLM: DispatchParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchParams:

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    parent_run_id: str = ""
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None


# LLM: WatchParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存监控参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class WatchParams:

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    parent_run_id: str = ""
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None
    interval: float = 30.0
    max_cycles: int = 0
    advance: bool = False
    force_lock: bool = False
    stop_file: str | Path | None = None


DISPATCH_PARAM_KEYS = tuple(field.name for field in fields(DispatchParams))
WATCH_PARAM_KEYS = tuple(field.name for field in fields(WatchParams))


# LLM: merge_dispatch_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新参数对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def merge_dispatch_params(
    params: DispatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> DispatchParams:
    if params is None:
        params = DispatchParams()
    elif not isinstance(params, DispatchParams):
        raise TypeError("dispatch_subagents() requires params: DispatchParams keyword argument")
    return _replace_bundle(params, DISPATCH_PARAM_KEYS, overrides or {})


# LLM: merge_watch_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新参数对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def merge_watch_params(
    params: WatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> WatchParams:
    if params is None:
        params = WatchParams()
    elif not isinstance(params, WatchParams):
        raise TypeError("watch_subagents() requires params: WatchParams keyword argument")
    return _replace_bundle(params, WATCH_PARAM_KEYS, overrides or {})


# LLM: dispatch_params_from_watch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进来自参数监控的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def dispatch_params_from_watch(params: WatchParams) -> DispatchParams:
    return DispatchParams(
        **{key: getattr(params, key) for key in DISPATCH_PARAM_KEYS}
    )


# LLM: _replace_bundle 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理replacebundle相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _replace_bundle(params, allowed_keys: tuple[str, ...], updates: dict[str, Any]):
    selected = {key: updates[key] for key in allowed_keys if key in updates}
    if not selected:
        return params
    return replace(params, **selected)


# LLM: DispatchContext 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存调度上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchContext:

    cfg: Any
    normalized_workflow_mode: str
    apply: bool
    planner: bool
    runner_instruction: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    take_over_by: str
    locked_files: list[str] | None
    router: Any  # CapabilityRouter – forward ref to avoid circular import at module level
    parent_run_id: str = ""
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None
    records: list = field(default_factory=list)


# LLM: RunnerBatchContext 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器batch上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class RunnerBatchContext:

    pending_runner_jobs: list
    runner_concurrency: int
    runner_timeout_seconds: float
    effective_runner_instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    records: list
