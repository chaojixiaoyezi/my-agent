# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any


# LLM: DispatchExecutionPlan is the canonical internal meaning of dry-run/execute mode.
# 类用途: 把预览、写状态、启动 runner 和 runner 上限收成一个执行计划，避免 apply/execute_runners/max_runners 散字段各处猜语义。
@dataclass(frozen=True)
class DispatchExecutionPlan:
    """Execution intent for one dispatch call."""

    preview_only: bool = True
    mutate_state: bool = False
    start_runners: bool = False
    max_runners: int = 1

    # LLM: from_internal_flags is the only compatibility adapter from legacy dispatch booleans.
    # 函数用途: 将 apply/execute_runners/max_runners 投影为统一执行计划，供旧 CLI 和新工具边界共用。
    @classmethod
    def from_internal_flags(
        cls,
        *,
        apply: bool,
        execute_runners: bool,
        max_runners: int,
    ) -> DispatchExecutionPlan:
        mutate_state = bool(apply)
        start_runners = bool(execute_runners and mutate_state)
        return cls(
            preview_only=not mutate_state,
            mutate_state=mutate_state,
            start_runners=start_runners,
            max_runners=max(0, int(max_runners or 0)),
        )


# LLM: DispatchParams is the single internal dispatch request object.
# 类用途: 保存一次调度推进所需的执行开关、作用域、runner 数量和补充指令；模型散字段只在工具边界转成这个对象。
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
    background_launch_id: str = ""
    execution_plan: DispatchExecutionPlan | None = None

    # LLM: DispatchParams.__post_init__ keeps legacy flags synchronized with the canonical execution plan.
    # 函数用途: 兼容旧 apply/execute_runners 调用，同时给新代码一个稳定 execution_plan 读取入口。
    def __post_init__(self) -> None:
        if self.execution_plan is None:
            self.execution_plan = DispatchExecutionPlan.from_internal_flags(
                apply=self.apply,
                execute_runners=self.execute_runners,
                max_runners=self.max_runners,
            )
            return
        self.apply = bool(self.execution_plan.mutate_state)
        self.execute_runners = bool(self.execution_plan.start_runners)
        self.max_runners = int(self.execution_plan.max_runners)


# LLM: WatchParams extends DispatchParams instead of duplicating dispatch fields.
# 类用途: 在同一套调度参数上增加后台 watch 循环自己的间隔、轮次、锁和停止文件。
@dataclass
class WatchParams(DispatchParams):

    interval: float = 30.0
    max_cycles: int = 0
    advance: bool = False
    force_lock: bool = False
    stop_file: str | Path | None = None


DISPATCH_PARAM_KEYS = tuple(field.name for field in fields(DispatchParams))
WATCH_PARAM_KEYS = tuple(field.name for field in fields(WatchParams))


# LLM: merge_dispatch_params applies trusted internal overrides to the dispatch request object.
# 函数用途: 保留已构造 DispatchParams，只替换调用方显式传入的字段。
def merge_dispatch_params(
    params: DispatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> DispatchParams:
    if params is None:
        params = DispatchParams()
    elif not isinstance(params, DispatchParams):
        raise TypeError("dispatch_subagents() requires params: DispatchParams keyword argument")
    updates = dict(overrides or {})
    if {"apply", "execute_runners", "max_runners"} & set(updates) and "execution_plan" not in updates:
        updates["execution_plan"] = None
    return _replace_bundle(params, DISPATCH_PARAM_KEYS, updates)


# LLM: merge_watch_params mirrors merge_dispatch_params for watch-only fields.
# 函数用途: 保留已构造 WatchParams，只替换调用方显式传入的字段。
def merge_watch_params(
    params: WatchParams | None = None,
    overrides: dict[str, Any] | None = None,
) -> WatchParams:
    if params is None:
        params = WatchParams()
    elif not isinstance(params, WatchParams):
        raise TypeError("watch_subagents() requires params: WatchParams keyword argument")
    return _replace_bundle(params, WATCH_PARAM_KEYS, overrides or {})


# LLM: dispatch_params_from_watch strips watch-loop controls before one dispatch cycle.
# 函数用途: 从 WatchParams 复制基础调度字段，确保 watch 循环和单次 dispatch 共用同一语义。
def dispatch_params_from_watch(params: WatchParams) -> DispatchParams:
    return DispatchParams(
        **{key: getattr(params, key) for key in DISPATCH_PARAM_KEYS}
    )


# LLM: _replace_bundle ignores unknown override keys so callers cannot invent hidden params.
# 函数用途: 只替换目标参数类声明过的字段。
def _replace_bundle(params, allowed_keys: tuple[str, ...], updates: dict[str, Any]):
    selected = {key: updates[key] for key in allowed_keys if key in updates}
    if not selected:
        return params
    return replace(params, **selected)


# LLM: DispatchContext is the resolved runtime state for one dispatch call.
# 类用途: 保存配置、scope、runner 开关和 records，使 dispatch 服务不用重新解析模型参数。
@dataclass
class DispatchContext:
    """Resolved dispatch state passed through one dispatch cycle."""

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
    background_launch_id: str = ""
    records: list = field(default_factory=list)
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)


# LLM: RunnerBatchContext carries runner execution controls after candidate selection.
# 类用途: 保存本批 runner 列表、并发数、超时和执行开关，供 runner batch 层直接消费。
@dataclass
class RunnerBatchContext:
    """Runner batch controls after dispatch candidate selection."""

    pending_runner_jobs: list
    runner_concurrency: int
    runner_timeout_seconds: float
    effective_runner_instruction: str
    execute_runners: bool
    max_cards: int
    probe: bool
    records: list
    execution_plan: DispatchExecutionPlan = field(default_factory=DispatchExecutionPlan)
