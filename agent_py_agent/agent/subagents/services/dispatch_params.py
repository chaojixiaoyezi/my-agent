# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Dispatch record params dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


# LLM: DispatchRecordParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存调度记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DispatchRecordParams:
    """Bundle of make_dispatch_record parameters."""
    step: str
    action: str
    run_id: str = ""
    dry_run: bool = True
    applied: bool = False
    ok: bool = True
    message: str = ""
    before_status: str = ""
    after_status: str = ""
    before_verification_status: str = ""
    after_verification_status: str = ""
    evidence_paths: list[str] | None = None


# LLM: DispatchWatchRecordParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存调度监控记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DispatchWatchRecordParams:
    """Bundle of make_dispatch_watch_record parameters."""
    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] | None = None
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] | None = None


# LLM: DispatchWatchHeartbeatParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存调度监控heartbeat参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class DispatchWatchHeartbeatParams:
    """Bundle of write_dispatch_watch_heartbeat parameters."""
    cycle: int
    status: str
    lock_path: str
    pid: int
    message: str = ""


# LLM: ParentPlannerRecordParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存父级规划器记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ParentPlannerRecordParams:
    """Bundle of make_parent_planner_record parameters."""
    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] | None = None
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] | None = None
    blockers: list[str] | None = None
    risks: list[str] | None = None
    notes: list[str] | None = None
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] | None = None
