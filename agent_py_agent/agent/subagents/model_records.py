# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Work-order, takeover, and channel-probe dataclasses."""

from dataclasses import dataclass, field


# LLM: WorkOrderValidation 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存workordervalidation字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class WorkOrderValidation:
    """Validation result for a subagent work-order directory."""

    run_id: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# LLM: TakeoverRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存takeover记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class TakeoverRecord:
    """Record of a parent/supervisor taking over a subagent task."""

    id: str
    run_id: str
    take_over_by: str
    reason: str
    locked_files: list[str] = field(default_factory=list)
    previous_owner: str = ""
    created_at: float = 0.0


# LLM: ChannelProbeCheck 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存通道probe检查字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class ChannelProbeCheck:
    """One health check item in a channel probe."""

    name: str
    ok: bool
    summary: str
    severity: str = "P1"
    evidence_path: str = ""
    error: str = ""
    created_at: float = 0.0


# LLM: ChannelProbeResult 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存通道probe结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ChannelProbeResult:
    """Channel probe result for one subagent run."""

    run_id: str
    channel_status: str
    checks: list[ChannelProbeCheck]
    task_dir: str = ""
    goal: str = ""
    created_at: float = 0.0


# LLM: ChannelProbeReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存通道probe报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class ChannelProbeReport:
    """Batch channel probe report."""

    generated_at: float
    summary: dict[str, int]
    results: list[ChannelProbeResult]
