# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Dispatch report models."""

from dataclasses import dataclass, field


# LLM: DispatchRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存调度记录字段，包括父级验收 auto-policy、auto-execution、follow-up 和 test report 摘要；关键副作用: 本身不执行输入输出，字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchRecord:
    """鐖朵唬鐞嗚皟搴﹀櫒鐨勪竴姝ュ璁¤褰曘€?"""

    id: str
    step: str
    action: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    before_status: str = ""
    after_status: str = ""
    before_verification_status: str = ""
    after_verification_status: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    # LLM: runner_* fields are refs-only child creation facts surfaced to parent agents.
    # 字段用途: 保存 runner 实际创建的下级数量、run id 和 role，避免父级误把 dispatch 记录数当成孩子数。
    runner_summary: str = ""
    runner_created_child_count: int = 0
    runner_created_child_ids: list[str] = field(default_factory=list)
    runner_created_roles: list[str] = field(default_factory=list)
    runner_child_status_counts: dict[str, int] = field(default_factory=dict)
    runner_unfinished_child_ids: list[str] = field(default_factory=list)
    runner_partial_success: bool = False
    parent_acceptance_policy_ref: str = ""
    parent_acceptance_policy_decision: str = ""
    parent_acceptance_policy_action: str = ""
    parent_acceptance_policy_would_execute: bool = False
    parent_acceptance_policy_executed: bool = False
    parent_acceptance_policy_execution_mode: str = ""
    parent_acceptance_policy_automatic_execution_allowed: bool = False
    parent_acceptance_policy_recommended_command: str = ""
    parent_acceptance_policy_preflight_status: str = ""
    parent_acceptance_policy_ready_for_automatic_execution: bool = False
    parent_acceptance_policy_preflight_blockers: list[str] = field(default_factory=list)
    parent_acceptance_auto_execution_ref: str = ""
    parent_acceptance_auto_execution_status: str = ""
    parent_acceptance_auto_execution_allowed: bool = False
    parent_acceptance_auto_execution_executed: bool = False
    parent_acceptance_auto_execution_guard_status: str = ""
    parent_acceptance_auto_execution_blocked_by: list[str] = field(default_factory=list)
    parent_acceptance_auto_execution_test_ref: str = ""
    parent_acceptance_auto_execution_test_total: int = 0
    parent_acceptance_auto_execution_test_failed: int = 0
    parent_acceptance_followup_ref: str = ""
    parent_acceptance_followup_status: str = ""
    parent_acceptance_followup_action: str = ""
    parent_acceptance_followup_command: str = ""
    parent_acceptance_followup_reason: str = ""
    created_at: float = 0.0


# LLM: DispatchReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存调度报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchReport:
    """鐖朵唬鐞嗚皟搴﹀櫒鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchRecord]


# LLM: DispatchWatchRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存调度监控记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchWatchRecord:
    """鐖朵唬鐞?watch 妯″紡鐨勪竴杞惊鐜褰曘€?"""

    id: str
    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] = field(default_factory=list)


# LLM: DispatchWatchReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存调度监控报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class DispatchWatchReport:
    """鐖朵唬鐞?watch 妯″紡鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchWatchRecord]
